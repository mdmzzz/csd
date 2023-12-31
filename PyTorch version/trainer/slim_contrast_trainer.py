import os
import math
from decimal import Decimal
from glob import glob
import datetime, time
from importlib import import_module
import numpy as np

# import lpips
import torchvision
from tensorboardX import SummaryWriter
import torch.nn as nn
import utils.utility as utility
from loss.contrast_loss import ContrastLoss
from loss.adversarial import Adversarial
from loss.perceptual import PerceptualLoss
from model.edsr import EDSR
from model.rcan import RCAN
from utils.niqe import niqe
from utils.ssim import calc_ssim


class SlimContrastiveTrainer:
    def __init__(self, args, loader, device, neg_loader=None):  #主要是参数配置
        self.model_str = args.model.lower()  #EDSR
        self.pic_path = f'./output/{self.model_str}/{args.model_filename}/'   #保存输出图片
        if not os.path.exists(self.pic_path):    #检查路径是否存在 不存在就创造一个文件夹
            self.makedirs = os.makedirs(self.pic_path)
        self.teacher_model = args.teacher_model   #把教师模型的存储
        self.checkpoint_dir = args.pre_train   #预训练网络模型路劲
        self.model_filename = args.model_filename  #模型名字
        self.model_filepath = f'{self.model_filename}.pth' #构建完整的路径名
        self.writer = SummaryWriter(f'log/{self.model_filename}')  #记录训练过程中的log

        self.start_epoch = -1
        self.device = device
        self.epochs = args.epochs
        self.init_lr = args.lr
        self.rgb_range = args.rgb_range
        self.scale = args.scale[0]
        self.stu_width_mult = args.stu_width_mult
        self.batch_size = args.batch_size
        self.neg_num = args.neg_num
        self.save_results = args.save_results
        self.self_ensemble = args.self_ensemble
        self.print_every = args.print_every
        self.best_psnr = 0
        self.best_psnr_epoch = -1

        self.loader = loader  #loder赋值给loader
        self.mean = [0.404, 0.436, 0.446]  #标准差
        self.std = [0.288, 0.263, 0.275]  #平均值

        self.build_model(args)
        self.upsampler = nn.Upsample(scale_factor=self.scale, mode='bicubic')  #上采样器
        self.optimizer = utility.make_optimizer(args, self.model)   #优化器 优化模型参数

        self.t_lambda = args.t_lambda
        self.contra_lambda = args.contra_lambda
        self.ad_lambda = args.ad_lambda  #是否启用自适应损失
        self.percep_lambda = args.percep_lambda
        self.t_detach = args.contrast_t_detach
        self.contra_loss = ContrastLoss(args.vgg_weight, args.d_func, self.t_detach)  #detach是否进行梯度传播
        self.l1_loss = nn.L1Loss()
        self.ad_loss = Adversarial(args, 'GAN')
        self.percep_loss = PerceptualLoss()
        self.t_l_remove = args.t_l_remove

    def train(self):
        self.model.train()  #模型调整为训练模式   训练模式会计算梯度 测试不会

        total_iter = (self.start_epoch+1)*len(self.loader.loader_train)
        for epoch in range(self.start_epoch + 1, self.epochs):
            if epoch >= self.t_l_remove:   #超过该轮次 教师的l1损失权重变为0  不在关注teacher的损失了
                self.t_lambda = 0

            starttime = datetime.datetime.now()   #获取当前的时间

            lrate = utility.adjust_learning_rate(self.optimizer, epoch, self.epochs, self.init_lr)
            print("[Epoch {}]\tlr:{}\t".format(epoch, lrate))
            psnr, t_psnr = 0.0, 0.0
            step = 0
            for batch, (lr, hr, _,) in enumerate(self.loader.loader_train):
                torchvision.cuda.empty_cache()  #清空GPU缓存
                step += 1    #增加总步数的值
                total_iter += 1  #总的迭代伦数
                lr = lr.to(self.device)
                hr = hr.to(self.device)

                self.optimizer.zero_grad()   #将梯度置为0
                teacher_sr = self.model(lr)   #得到教师模型的结果
                
                student_sr = self.model(lr, self.stu_width_mult)  #得到学生模型的结果
                l1_loss = self.l1_loss(hr, student_sr)
                teacher_l1_loss = self.l1_loss(hr, teacher_sr)

                bic_sample = lr[torchvision.randperm(self.neg_num), :, :, :]   #随机选这么多个负例的构建
                bic_sample = self.upsampler(bic_sample)  #对于数据进行上采样
                contras_loss = 0.0

                if self.neg_num > 0:  #计算对比损失   neg是负样本数量
                    contras_loss = self.contra_loss(teacher_sr, student_sr, bic_sample)

                loss = l1_loss + self.contra_lambda * contras_loss + self.t_lambda * teacher_l1_loss   #超过一定就不在计算教师的损失
                if self.ad_lambda > 0:
                    ad_loss = self.ad_loss(student_sr, hr)   #可能的自适应损失  需要判断是否大于0
                    loss += self.ad_lambda * ad_loss
                    self.writer.add_scalar('Train/Ad_loss', ad_loss, total_iter)
                if self.percep_lambda > 0:
                    percep_loss = self.percep_loss(hr, student_sr)   #感知损失
                    loss += self.percep_lambda * percep_loss
                    self.writer.add_scalar('Train/Percep_loss', percep_loss, total_iter)
                
                loss.backward()
                self.optimizer.step()

                self.writer.add_scalar('Train/L1_loss', l1_loss, total_iter)
                self.writer.add_scalar('Train/Contras_loss', contras_loss, total_iter)
                self.writer.add_scalar('Train/Teacher_l1_loss', teacher_l1_loss, total_iter)
                self.writer.add_scalar('Train/Total_loss', loss, total_iter)

                student_sr = utility.quantize(student_sr, self.rgb_range)
                psnr += utility.calc_psnr(student_sr, hr, self.scale, self.rgb_range)
                teacher_sr = utility.quantize(teacher_sr, self.rgb_range)
                t_psnr += utility.calc_psnr(teacher_sr, hr, self.scale, self.rgb_range)   #得到效果  PSNR
                if (batch + 1) % self.print_every == 0:
                    print(
                        f"[Epoch {epoch}/{self.epochs}] [Batch {batch * self.batch_size}/{len(self.loader.loader_train.dataset)}] "
                        f"[psnr {psnr / step}]"
                        f"[t_psnr {t_psnr / step}]"
                    )
                    utility.save_results(f'result_{batch}', hr, self.scale, width=1, rgb_range=self.rgb_range,
                                         postfix='hr', dir=self.pic_path)
                    utility.save_results(f'result_{batch}', teacher_sr, self.scale, width=1, rgb_range=self.rgb_range,
                                         postfix='t_sr', dir=self.pic_path)
                    utility.save_results(f'result_{batch}', student_sr, self.scale, width=1, rgb_range=self.rgb_range,
                                         postfix='s_sr', dir=self.pic_path)

            print(f"training PSNR @epoch {epoch}: {psnr / step}")

            test_psnr = self.test(self.stu_width_mult) #保存最佳的轮次和参数值
            if test_psnr > self.best_psnr:
                print(f"saving models @epoch {epoch} with psnr: {test_psnr}")
                self.best_psnr = test_psnr
                self.best_psnr_epoch = epoch
                torchvision.save({
                    'epoch': epoch,
                    'model_state_dict': self.model.state_dict(),
                    'optimizer_state_dict': self.optimizer.state_dict(),
                    'best_psnr': self.best_psnr,
                    'best_psnr_epoch': self.best_psnr_epoch,
                }, f'{self.checkpoint_dir}{self.model_filepath}')

            endtime = datetime.datetime.now()
            cost = (endtime - starttime).seconds
            print(f"time of epoch{epoch}: {cost}")

    def test(self, width_mult=1):
        self.model.eval()
        with torchvision.no_grad():
            psnr = 0
            niqe_score = 0
            ssim = 0
            t0 = time.time()

            starttime = datetime.datetime.now()
            for d in self.loader.loader_test:
                for lr, hr, filename in d:
                    lr = lr.to(self.device)
                    hr = hr.to(self.device)

                    x = [lr]
                    for tf in 'v', 'h', 't':
                        x.extend([utility.transform(_x, tf, self.device) for _x in x])
                    op = ['', 'v', 'h', 'hv', 't', 'tv', 'th', 'thv']

                    if self.self_ensemble:
                        res = self.model(lr, width_mult)
                        for i in range(1, len(x)):
                            _x = x[i]
                            _sr = self.model(_x, width_mult)
                            for _op in op[i]:
                                _sr = utility.transform(_sr, _op, self.device)
                            res = torchvision.cat((res, _sr), 0)
                        sr = torchvision.mean(res, 0).unsqueeze(0)
                    else:
                        sr = self.model(lr, width_mult)

                    sr = utility.quantize(sr, self.rgb_range)
                    if self.save_results:
                    #     if not os.path.exists(f'./output/test/{self.model_str}/{self.model_filename}'):
                    #         self.makedirs = os.makedirs(f'./output/test/{self.model_str}/{self.model_filename}')
                        utility.save_results(str(filename), sr, self.scale, width_mult,
                                             self.rgb_range, 'SR')

                    psnr += utility.calc_psnr(sr, hr, self.scale, self.rgb_range, dataset=d)
                    niqe_score += niqe(sr.squeeze(0).permute(1, 2, 0).cpu().numpy())
                    ssim += calc_ssim(sr, hr, self.scale, dataset=d)

                psnr /= len(d)
                niqe_score /= len(d)
                ssim /= len(d)
                print(width_mult, d.dataset.name, psnr, niqe_score, ssim)

                endtime = datetime.datetime.now()
                cost = (endtime - starttime).seconds
                t1 = time.time()
                total_time = (t1 - t0)
                print(f"time of test: {total_time}")
                return psnr

    def build_model(self, args):
        m = import_module('model.' + self.model_str)  #edsr modual.edsr
        self.model = getattr(m, self.model_str.upper())(args).to(self.device)  #获取类对象
        self.model = nn.DataParallel(self.model, device_ids=range(args.n_GPUs))  #放到多个GPU里面训练
        self.load_model()

        # test teacher
        # self.test()

    def load_model(self):
        checkpoint_dir = self.checkpoint_dir
        print(f"[*] Load model from {checkpoint_dir}")
        if not os.path.exists(checkpoint_dir):
            self.makedirs = os.makedirs(checkpoint_dir)

        if not os.listdir(checkpoint_dir):
            print(f"[!] No checkpoint in {checkpoint_dir}")
            return

        model = glob(os.path.join(checkpoint_dir, self.model_filepath))

        no_student = False
        if not model:
            no_student = True
            print(f"[!] No checkpoint ")
            print("Loading pre-trained teacher model")
            model = glob(self.teacher_model)
            if not model:
                print(f"[!] No teacher model ")
                return

        model_state_dict = torchvision.load(model[0])
        if not no_student:
            self.start_epoch = model_state_dict['epoch']
            self.best_psnr = model_state_dict['best_psnr']
            self.best_psnr_epoch = model_state_dict['best_psnr_epoch']

        self.model.load_state_dict(model_state_dict['model_state_dict'], False)