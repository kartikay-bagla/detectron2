import math

from detectron2.modeling  import BACKBONE_REGISTRY
from detectron2.modeling.backbone import Backbone

from utils.google_utils import *
from utils.layers import *
from utils.parse_config import *
from utils import torch_utils

ONNX_EXPORT = False

def create_modules(module_defs, img_size, cfg):
    # Constructs module list of layer blocks from module configuration in module_defs

    img_size = [img_size] * 2 if isinstance(img_size, int) else img_size  # expand if necessary
    _ = module_defs.pop(0)  # cfg training hyperparams (unused)
    output_filters = [3]  # input channels
    module_list = nn.ModuleList()
    routs = []  # list of layers which rout to deeper layers
    yolo_index = -1

    for i, mdef in enumerate(module_defs):
        modules = nn.Sequential()

        if mdef['type'] == 'convolutional':
            bn = mdef['batch_normalize']
            filters = mdef['filters']
            k = mdef['size']  # kernel size
            stride = mdef['stride'] if 'stride' in mdef else (mdef['stride_y'], mdef['stride_x'])
            if isinstance(k, int):  # single-size conv
                modules.add_module('Conv2d', nn.Conv2d(in_channels=output_filters[-1],
                                                       out_channels=filters,
                                                       kernel_size=k,
                                                       stride=stride,
                                                       padding=k // 2 if mdef['pad'] else 0,
                                                       groups=mdef['groups'] if 'groups' in mdef else 1,
                                                       bias=not bn))
            else:  # multiple-size conv
                modules.add_module('MixConv2d', MixConv2d(in_ch=output_filters[-1],
                                                          out_ch=filters,
                                                          k=k,
                                                          stride=stride,
                                                          bias=not bn))

            if bn:
                modules.add_module('BatchNorm2d', nn.BatchNorm2d(filters, momentum=0.03, eps=1E-4))
            else:
                routs.append(i)  # detection output (goes into yolo layer)

            if mdef['activation'] == 'leaky':  # activation study https://github.com/ultralytics/yolov3/issues/441
                modules.add_module('activation', nn.LeakyReLU(0.1, inplace=True))
            elif mdef['activation'] == 'swish':
                modules.add_module('activation', Swish())
            elif mdef['activation'] == 'mish':
                modules.add_module('activation', Mish())
            elif mdef['activation'] == 'emb':
                modules.add_module('activation', F.normalize())
            elif mdef['activation'] == 'logistic':
                modules.add_module('activation', nn.Sigmoid())
            elif mdef['activation'] == 'silu':
                modules.add_module('activation', nn.SiLU())

        elif mdef['type'] == 'deformableconvolutional':
            bn = mdef['batch_normalize']
            filters = mdef['filters']
            k = mdef['size']  # kernel size
            stride = mdef['stride'] if 'stride' in mdef else (mdef['stride_y'], mdef['stride_x'])
            if isinstance(k, int):  # single-size conv
                modules.add_module('DeformConv2d', DeformConv2d(output_filters[-1],
                                                       filters,
                                                       kernel_size=k,
                                                       padding=k // 2 if mdef['pad'] else 0,
                                                       stride=stride,
                                                       bias=not bn,
                                                       modulation=True))
            else:  # multiple-size conv
                modules.add_module('MixConv2d', MixConv2d(in_ch=output_filters[-1],
                                                          out_ch=filters,
                                                          k=k,
                                                          stride=stride,
                                                          bias=not bn))

            if bn:
                modules.add_module('BatchNorm2d', nn.BatchNorm2d(filters, momentum=0.03, eps=1E-4))
            else:
                routs.append(i)  # detection output (goes into yolo layer)

            if mdef['activation'] == 'leaky':  # activation study https://github.com/ultralytics/yolov3/issues/441
                modules.add_module('activation', nn.LeakyReLU(0.1, inplace=True))
            elif mdef['activation'] == 'swish':
                modules.add_module('activation', Swish())
            elif mdef['activation'] == 'mish':
                modules.add_module('activation', Mish())
            elif mdef['activation'] == 'silu':
                modules.add_module('activation', nn.SiLU())
                
        elif mdef['type'] == 'dropout':
            p = mdef['probability']
            modules = nn.Dropout(p)

        elif mdef['type'] == 'avgpool':
            modules = GAP()

        elif mdef['type'] == 'silence':
            filters = output_filters[-1]
            modules = Silence()

        elif mdef['type'] == 'scale_channels':  # nn.Sequential() placeholder for 'shortcut' layer
            layers = mdef['from']
            filters = output_filters[-1]
            routs.extend([i + l if l < 0 else l for l in layers])
            modules = ScaleChannel(layers=layers)

        elif mdef['type'] == 'sam':  # nn.Sequential() placeholder for 'shortcut' layer
            layers = mdef['from']
            filters = output_filters[-1]
            routs.extend([i + l if l < 0 else l for l in layers])
            modules = ScaleSpatial(layers=layers)

        elif mdef['type'] == 'BatchNorm2d':
            filters = output_filters[-1]
            modules = nn.BatchNorm2d(filters, momentum=0.03, eps=1E-4)
            if i == 0 and filters == 3:  # normalize RGB image
                # imagenet mean and var https://pytorch.org/docs/stable/torchvision/models.html#classification
                modules.running_mean = torch.tensor([0.485, 0.456, 0.406])
                modules.running_var = torch.tensor([0.0524, 0.0502, 0.0506])

        elif mdef['type'] == 'maxpool':
            k = mdef['size']  # kernel size
            stride = mdef['stride']
            maxpool = nn.MaxPool2d(kernel_size=k, stride=stride, padding=(k - 1) // 2)
            if k == 2 and stride == 1:  # yolov3-tiny
                modules.add_module('ZeroPad2d', nn.ZeroPad2d((0, 1, 0, 1)))
                modules.add_module('MaxPool2d', maxpool)
            else:
                modules = maxpool

        elif mdef['type'] == 'local_avgpool':
            k = mdef['size']  # kernel size
            stride = mdef['stride']
            avgpool = nn.AvgPool2d(kernel_size=k, stride=stride, padding=(k - 1) // 2)
            if k == 2 and stride == 1:  # yolov3-tiny
                modules.add_module('ZeroPad2d', nn.ZeroPad2d((0, 1, 0, 1)))
                modules.add_module('AvgPool2d', avgpool)
            else:
                modules = avgpool

        elif mdef['type'] == 'upsample':
            if ONNX_EXPORT:  # explicitly state size, avoid scale_factor
                g = (yolo_index + 1) * 2 / 32  # gain
                modules = nn.Upsample(size=tuple(int(x * g) for x in img_size))  # img_size = (320, 192)
            else:
                modules = nn.Upsample(scale_factor=mdef['stride'])

        elif mdef['type'] == 'route':  # nn.Sequential() placeholder for 'route' layer
            layers = mdef['layers']
            filters = sum([output_filters[l + 1 if l > 0 else l] for l in layers])
            routs.extend([i + l if l < 0 else l for l in layers])
            modules = FeatureConcat(layers=layers)

        elif mdef['type'] == 'route2':  # nn.Sequential() placeholder for 'route' layer
            layers = mdef['layers']
            filters = sum([output_filters[l + 1 if l > 0 else l] for l in layers])
            routs.extend([i + l if l < 0 else l for l in layers])
            modules = FeatureConcat2(layers=layers)

        elif mdef['type'] == 'route3':  # nn.Sequential() placeholder for 'route' layer
            layers = mdef['layers']
            filters = sum([output_filters[l + 1 if l > 0 else l] for l in layers])
            routs.extend([i + l if l < 0 else l for l in layers])
            modules = FeatureConcat3(layers=layers)

        elif mdef['type'] == 'route_lhalf':  # nn.Sequential() placeholder for 'route' layer
            layers = mdef['layers']
            filters = sum([output_filters[l + 1 if l > 0 else l] for l in layers])//2
            routs.extend([i + l if l < 0 else l for l in layers])
            modules = FeatureConcat_l(layers=layers)

        elif mdef['type'] == 'shortcut':  # nn.Sequential() placeholder for 'shortcut' layer
            layers = mdef['from']
            filters = output_filters[-1]
            routs.extend([i + l if l < 0 else l for l in layers])
            modules = WeightedFeatureFusion(layers=layers, weight='weights_type' in mdef)

        elif mdef['type'] == 'reorg3d':  # yolov3-spp-pan-scale
            pass

        elif mdef['type'] == 'reorg':  # yolov3-spp-pan-scale
            filters = 4 * output_filters[-1]
            modules.add_module('Reorg', Reorg())

        elif mdef['type'] == 'yolo':
            yolo_index += 1
            stride = [8, 16, 32, 64, 128]  # P3, P4, P5, P6, P7 strides
            if any(x in cfg for x in ['yolov4-tiny', 'fpn', 'yolov3']):  # P5, P4, P3 strides
                stride = [32, 16, 8]
            layers = mdef['from'] if 'from' in mdef else []
            modules = YOLOLayer(anchors=mdef['anchors'][mdef['mask']],  # anchor list
                                nc=mdef['classes'],  # number of classes
                                img_size=img_size,  # (416, 416)
                                yolo_index=yolo_index,  # 0, 1, 2...
                                layers=layers,  # output layers
                                stride=stride[yolo_index])

            # Initialize preceding Conv2d() bias (https://arxiv.org/pdf/1708.02002.pdf section 3.3)
            try:
                j = layers[yolo_index] if 'from' in mdef else -1
                bias_ = module_list[j][0].bias  # shape(255,)
                bias = bias_[:modules.no * modules.na].view(modules.na, -1)  # shape(3,85)
                #bias[:, 4] += -4.5  # obj
                bias.data[:, 4] += math.log(8 / (640 / stride[yolo_index]) ** 2)  # obj (8 objects per 640 image)
                bias.data[:, 5:] += math.log(0.6 / (modules.nc - 0.99))  # cls (sigmoid(p) = 1/nc)
                module_list[j][0].bias = torch.nn.Parameter(bias_, requires_grad=bias_.requires_grad)
                
                #j = [-2, -5, -8]
                #for sj in j:
                #    bias_ = module_list[sj][0].bias
                #    bias = bias_[:modules.no * 1].view(1, -1)
                #    bias.data[:, 4] += math.log(8 / (640 / stride[yolo_index]) ** 2)
                #    bias.data[:, 5:] += math.log(0.6 / (modules.nc - 0.99))
                #    module_list[sj][0].bias = torch.nn.Parameter(bias_, requires_grad=bias_.requires_grad)
            except:
                print('WARNING: smart bias initialization failure.')

        else:
            print('Warning: Unrecognized Layer Type: ' + mdef['type'])

        # Register module list and number of output filters
        module_list.append(modules)
        output_filters.append(filters)

    routs_binary = [False] * (i + 1)
    for i in routs:
        routs_binary[i] = True
    return module_list, routs_binary


class YOLOLayer(nn.Module):
    def __init__(self, anchors, nc, img_size, yolo_index, layers, stride):
        super(YOLOLayer, self).__init__()
        self.anchors = torch.Tensor(anchors)
        self.index = yolo_index  # index of this layer in layers
        self.layers = layers  # model output layer indices
        self.stride = stride  # layer stride
        self.nl = len(layers)  # number of output layers (3)
        self.na = len(anchors)  # number of anchors (3)
        self.nc = nc  # number of classes (80)
        self.no = nc + 5  # number of outputs (85)
        self.nx, self.ny, self.ng = 0, 0, 0  # initialize number of x, y gridpoints
        self.anchor_vec = self.anchors / self.stride
        self.anchor_wh = self.anchor_vec.view(1, self.na, 1, 1, 2)

        if ONNX_EXPORT:
            self.training = False
            self.create_grids((img_size[1] // stride, img_size[0] // stride))  # number x, y grid points

    def create_grids(self, ng=(13, 13), device='cpu'):
        self.nx, self.ny = ng  # x and y grid size
        self.ng = torch.tensor(ng, dtype=torch.float)

        yv, xv = torch.meshgrid([torch.arange(self.ny, device=device), torch.arange(self.nx, device=device)])
        self.grid = torch.stack((xv, yv), 2).view((1, 1, self.ny, self.nx, 2)).float()

        if self.anchor_vec.device != device:
            self.anchor_vec = self.anchor_vec.to(device)
            self.anchor_wh = self.anchor_wh.to(device)

    def forward(self, p, out):
        ASFF = False  # https://arxiv.org/abs/1911.09516
        if ASFF:
            i, n = self.index, self.nl  # index in layers, number of layers
            p = out[self.layers[i]]
            bs, _, ny, nx = p.shape  # bs, 255, 13, 13
            if (self.nx, self.ny) != (nx, ny):
                self.create_grids((nx, ny), p.device)

            # outputs and weights
            # w = F.softmax(p[:, -n:], 1)  # normalized weights
            w = torch.sigmoid(p[:, -n:]) * (2 / n)  # sigmoid weights (faster)
            # w = w / w.sum(1).unsqueeze(1)  # normalize across layer dimension

            # weighted ASFF sum
            p = out[self.layers[i]][:, :-n] * w[:, i:i + 1]
            for j in range(n):
                if j != i:
                    p += w[:, j:j + 1] * \
                         F.interpolate(out[self.layers[j]][:, :-n], size=[ny, nx], mode='bilinear', align_corners=False)

        elif ONNX_EXPORT:
            bs = 1  # batch size
        else:
            bs, _, ny, nx = p.shape  # bs, 255, 13, 13
            if (self.nx, self.ny) != (nx, ny):
                self.create_grids((nx, ny), p.device)

        # p.view(bs, 255, 13, 13) -- > (bs, 3, 13, 13, 85)  # (bs, anchors, grid, grid, classes + xywh)
        p = p.view(bs, self.na, self.no, self.ny, self.nx).permute(0, 1, 3, 4, 2).contiguous()  # prediction

        io = p.sigmoid()
        io[..., :2] = (io[..., :2] * 2. - 0.5 + self.grid)
        io[..., 2:4] = (io[..., 2:4] * 2) ** 2 * self.anchor_wh
        io[..., :4] *= self.stride
        return io.view(bs, -1, self.no), p  # view [1, 3, 13, 13, 85] as [1, 507, 85]


class Darknet(nn.Module):
    # YOLOv3 object detection model

    def __init__(self, cfg, img_size=(416, 416)):
        super(Darknet, self).__init__()

        self.module_defs = parse_model_cfg(cfg)
        self.module_list, self.routs = create_modules(self.module_defs, img_size, cfg)
        self.yolo_layers = get_yolo_layers(self)
        # torch_utils.initialize_weights(self)

        # Darknet Header https://github.com/AlexeyAB/darknet/issues/2914#issuecomment-496675346
        self.version = np.array([0, 2, 5], dtype=np.int32)  # (int32) version info: major, minor, revision
        self.seen = np.array([0], dtype=np.int64)  # (int64) number of images seen during training

        self.output_layers = {
            "p1": 54,
            "p2": 83,
            "p3": 117,
        }

    def forward(self, x):
        """Returns preds, feature dict, unformatted preds"""
        return self.forward_once(x)

    def forward_once(self, x):
        yolo_out, out = [], []

        for i, module in enumerate(self.module_list):
            name = module.__class__.__name__
            if name in ['WeightedFeatureFusion', 'FeatureConcat', 'FeatureConcat2', 'FeatureConcat3', 'FeatureConcat_l', 'ScaleChannel', 'ScaleSpatial']:  # sum, concat
                x = module(x, out)  # WeightedFeatureFusion(), FeatureConcat()
            elif name == 'YOLOLayer':
                yolo_out.append(module(x, out))
            else:  # run module directly, i.e. mtype = 'convolutional', 'upsample', 'maxpool', 'batchnorm2d' etc.
                x = module(x)

            # add yolo feature layers to output
            out.append(x if self.routs[i] or i in self.output_layers.values() else [])

        yolo_features = {k: out[i] for k, i in self.output_layers.items()}

        x, p = zip(*yolo_out)  # inference output, training output
        x = torch.cat(x, 1)  # cat yolo outputs

        x = non_max_suppression(x, 0.5, 0.6)

        return x, yolo_features, p

    def fuse(self):
        # Fuse Conv2d + BatchNorm2d layers throughout model
        print('Fusing layers...')
        fused_list = nn.ModuleList()
        for a in list(self.children())[0]:
            if isinstance(a, nn.Sequential):
                for i, b in enumerate(a):
                    if isinstance(b, nn.modules.batchnorm.BatchNorm2d):
                        # fuse this bn layer with the previous conv2d layer
                        conv = a[i - 1]
                        fused = torch_utils.fuse_conv_and_bn(conv, b)
                        a = nn.Sequential(fused, *list(a.children())[i + 1:])
                        break
            fused_list.append(a)
        self.module_list = fused_list
        self.info() if not ONNX_EXPORT else None  # yolov3-spp reduced from 225 to 152 layers

    def info(self, verbose=False):
        torch_utils.model_info(self, verbose)

    def load_darknet_weights(self, weights, cutoff=-1):
        # Parses and loads the weights stored in 'weights'

        # Establish cutoffs (load layers between 0 and cutoff. if cutoff = -1 all are loaded)
        file = Path(weights).name
        if file == 'darknet53.conv.74':
            cutoff = 75
        elif file == 'yolov3-tiny.conv.15':
            cutoff = 15

        # Read weights file
        with open(weights, 'rb') as f:
            # Read Header https://github.com/AlexeyAB/darknet/issues/2914#issuecomment-496675346
            self.version = np.fromfile(f, dtype=np.int32, count=3)  # (int32) version info: major, minor, revision
            self.seen = np.fromfile(f, dtype=np.int64, count=1)  # (int64) number of images seen during training

            weights = np.fromfile(f, dtype=np.float32)  # the rest are weights

        ptr = 0
        for i, (mdef, module) in enumerate(zip(self.module_defs[:cutoff], self.module_list[:cutoff])):
            if mdef['type'] == 'convolutional':
                conv = module[0]
                if mdef['batch_normalize']:
                    # Load BN bias, weights, running mean and running variance
                    bn = module[1]
                    nb = bn.bias.numel()  # number of biases
                    # Bias
                    bn.bias.data.copy_(torch.from_numpy(weights[ptr:ptr + nb]).view_as(bn.bias))
                    ptr += nb
                    # Weight
                    bn.weight.data.copy_(torch.from_numpy(weights[ptr:ptr + nb]).view_as(bn.weight))
                    ptr += nb
                    # Running Mean
                    bn.running_mean.data.copy_(torch.from_numpy(weights[ptr:ptr + nb]).view_as(bn.running_mean))
                    ptr += nb
                    # Running Var
                    bn.running_var.data.copy_(torch.from_numpy(weights[ptr:ptr + nb]).view_as(bn.running_var))
                    ptr += nb
                else:
                    # Load conv. bias
                    nb = conv.bias.numel()
                    conv_b = torch.from_numpy(weights[ptr:ptr + nb]).view_as(conv.bias)
                    conv.bias.data.copy_(conv_b)
                    ptr += nb
                # Load conv. weights
                nw = conv.weight.numel()  # number of weights
                conv.weight.data.copy_(torch.from_numpy(weights[ptr:ptr + nw]).view_as(conv.weight))
                ptr += nw

    def save_weights(self, path='model.weights', cutoff=-1):
        # Converts a PyTorch model to Darket format (*.pt to *.weights)
        # Note: Does not work if model.fuse() is applied
        with open(path, 'wb') as f:
            # Write Header https://github.com/AlexeyAB/darknet/issues/2914#issuecomment-496675346
            self.version.tofile(f)  # (int32) version info: major, minor, revision
            self.seen.tofile(f)  # (int64) number of images seen during training

            # Iterate through layers
            for i, (mdef, module) in enumerate(zip(self.module_defs[:cutoff], self.module_list[:cutoff])):
                if mdef['type'] == 'convolutional':
                    conv_layer = module[0]
                    # If batch norm, load bn first
                    if mdef['batch_normalize']:
                        bn_layer = module[1]
                        bn_layer.bias.data.cpu().numpy().tofile(f)
                        bn_layer.weight.data.cpu().numpy().tofile(f)
                        bn_layer.running_mean.data.cpu().numpy().tofile(f)
                        bn_layer.running_var.data.cpu().numpy().tofile(f)
                    # Load conv bias
                    else:
                        conv_layer.bias.data.cpu().numpy().tofile(f)
                    # Load conv weights
                    conv_layer.weight.data.cpu().numpy().tofile(f)

def get_yolo_layers(model):
    return [i for i, m in enumerate(model.module_list) if m.__class__.__name__ in ['YOLOLayer', 'JDELayer']]  # [89, 101, 113]

class YOLOBackbone(Backbone):
    def __init__(self, cfg, input_shape):
        super(YOLOBackbone, self).__init__()
        self.model = Darknet(cfg, input_shape)
        self.model.load_darknet_weights("yolov4.weights")
        self._out_features = ["p1", "p2", "p3"]
        self._out_feature_channels = {"p1": 256, "p2": 512, "p3": 1024}
        self._out_feature_strides = {"p1": 4, "p2": 8, "p3": 16}

    def forward(self, x):
        return self.model(x)

@BACKBONE_REGISTRY.register()
def build_yolo_backbone(cfg, input_shape):
    return YOLOBackbone("yolov4.cfg", (input_shape.width, input_shape.height))