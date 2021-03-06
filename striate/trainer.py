from pycuda import gpuarray, driver
from striate import util, layer
from striate.fastnet import FastNet, AdaptiveFastNet
from striate.layer import TRAIN, TEST
from striate.parser import parse_config_file
from striate.scheduler import Scheduler
from striate.util import divup, timer, load
from data import DataProvider, ImageNetDataProvider
import argparse
import cPickle
import glob
import numpy as np
import os
import pprint
import re
import sys
import time

class DataDumper(object):
  def __init__(self, target_path, max_mem_size = 500e5):
    self.target_path = target_path
    self.data = []
    self.sz = 0
    self.count = 0
    self.max_mem_size = max_mem_size

    util.log('dumper establised')
    util.log('target path:    %s', self.target_path)
    util.log('max_memory:     %s', self.max_mem_size)

  def add(self, data):
    for k, v in data.iteritems():
      self.sz += np.prod(v.shape)
    self.data.append(data)

    if self.sz > self.max_mem_size:
      self.flush()

  def flush(self):
    if self.sz == 0:
      return

    out = {}
    for k in self.data[0].keys():
      items = [d[k] for d in self.data]
      out[k] = np.concatenate(items, axis=0)

    filename = '%s.%d' % (self.target_path, self.count)
    with open(filename, 'w') as f:
      cPickle.dump(out, f, -1)

    util.log('Wrote layer dump to %s', filename)
    self.data = []
    self.sz = 0
    self.count += 1

  def get_count(self):return self.count





class MemoryDataHolder(object):
  def __init__(self, single_memory_size = 50e6, total_memory_size = 2e9):
    self.single_memory_size = single_memory_size
    self.total_memory_size = total_memory_size
    self.single_data_size = 0
    self.total_data_size = 0
    self.count = 0
    self.data = []
    self.memory_chunk = []

    util.log('memory data holder establised')
    util.log('total memory size:    %s', self.total_memory_size)
    util.log('single memory size:   %s', self.single_memory_size)


  def add(self, data):
    for k, v in data.iteritems():
      self.single_data_size += v.nbytes
      self.total_data_size += v.nbytes
    self.data.append(data)

    if self.total_data_size > self.total_memory_size:
      self.cut_off_chunk()

    if self.single_data_size > self.single_memory_size:
      self.flush()


  def flush(self):
    if self.single_data_size == 0:
      return

    dic = {}
    for k in self.data[0].keys():
      items= [d[k] for d in self.data]
      dic[k] = np.concatenate(items, axis = 0)

    self.memory_chunk.append(dic)

    util.log('add another memory chunk')
    util.log('memory chunk size:    %s', self.single_data_size)
    util.log('total data size:    %s', self.total_data_size)

    self.data = []
    self.single_data_size = 0
    self.count += 1

  def cut_off_chunk(self):
    if len(self.memory_chunk) == 0:
      util.log('There is no chunk to cut off')
      return

    size = 0
    for k, v, in self.memory_chunk[0].iteritems():
      size += self.memory_chunk[0][k].nbytes

    del self.memory_chunk[0]
    self.total_data_size -= size
    util.log('drop off the first memory chunk')
    util.log('droped chunk size:    %s', size)
    util.log('total data size:      %s', self.total_data_size)

  def finish_push(self):
    self.flush()

  def get_count(self):
    return self.count




class CheckpointDumper(object):
  def __init__(self, checkpoint_dir, test_id):
    self.checkpoint_dir = checkpoint_dir

    if not os.path.exists(self.checkpoint_dir):
      os.system('mkdir -p \'%s\'' % self.checkpoint_dir)

    self.test_id = test_id
    self.regex = re.compile('^test%d-(\d+)$' % self.test_id)

    cp_pattern = self.checkpoint_dir + '/test%d-*' % self.test_id
    cp_files = glob.glob(cp_pattern)

    if not cp_files:
      self.checkpoint = None
      self.checkpoint_file = None
    else:
      self.checkpoint_file = sorted(cp_files, key=os.path.getmtime)[-1]
      util.log('Loading from checkpoint file: %s', self.checkpoint_file)
      self.checkpoint = util.load(self.checkpoint_file)

  def get_checkpoint(self):
    return self.checkpoint


  def dump(self, checkpoint, suffix):
    self.checkpoint = checkpoint
    saved_filename = [f for f in os.listdir(self.checkpoint_dir) if self.regex.match(f)]
    for f in saved_filename:
      os.remove(os.path.join(self.checkpoint_dir, f))
    checkpoint_filename = "test%d-%d" % (self.test_id, suffix)
    self.checkpoint_file = os.path.join(self.checkpoint_dir, checkpoint_filename)
    print >> sys.stderr,  self.checkpoint_file
    with open(self.checkpoint_file, 'w') as f:
      cPickle.dump(checkpoint, f, protocol=-1)
    util.log('save file finished')



# Trainer should take: (training dp, test dp, fastnet, checkpoint dir)
class Trainer:
  def __init__(self, checkpoint_dumper, train_dp, test_dp, batch_size, net = None, **kw):
    self.checkpoint_dumper = checkpoint_dumper
    self.train_dp = train_dp
    self.test_dp = test_dp
    self.batch_size = batch_size
    self.net = net
    self.curr_batch = self.curr_epoch = 0

    for k, v in kw.iteritems():
      setattr(self, k, v)


    checkpoint = self.checkpoint_dumper.get_checkpoint()
    if checkpoint:
      self.train_outputs = checkpoint['model_state']['train_outputs']
      self.test_outputs = checkpoint['model_state']['test_outputs']
    else:
      self.train_outputs = []
      self.test_outputs = []

    if self.output_dir:
      self.train_output_filename = os.path.join(self.output_dir, 'train-data.pickle')
      self.test_output_filename = os.path.join(self.output_dir, 'test-data.pickle')
    else:
      self.train_output_filename = ''
      self.test_output_filename = ''
    self.init_output_dumper()
    self._finish_init()

  def _finish_init(self):
    pass

  def init_output_dumper(self):
    if self.train_output_filename:
      self.train_dumper = DataDumper(self.train_output_filename)
    if self.test_output_filename:
      self.test_dumper = DataDumper(self.test_output_filename)
    #self.train_dumper = MemoryDataHolder()
    #self.test_dumper = MemoryDataHolder()


  def init_data_provider(self):
    self.train_dp.reset()
    self.test_dp.reset()


  def save_checkpoint(self):
    model = {}
    model['layers'] = self.net.get_dumped_layers()
    model['train_outputs'] = self.train_outputs
    model['test_outputs'] = self.test_outputs

    dic = {'model_state': model, 'op':None}
    print >> sys.stderr,  '---- save checkpoint ----'
    self.print_net_summary()
    self.checkpoint_dumper.dump(checkpoint = dic, suffix = self.curr_epoch)


  def adjust_lr(self):
    print >> sys.stderr,  '---- adjust learning rate ----'
    self.net.adjust_learning_rate(self.factor)

  def get_test_error(self):
    start = time.time()
    test_data = self.test_dp.get_next_batch(self.batch_size)

    input, label = test_data.data, test_data.labels
    self.net.train_batch(input, label, TEST)
    self._capture_test_data()

    cost , correct, numCase, = self.net.get_batch_information()
    self.test_outputs += [({'logprob': [cost, 1 - correct]}, numCase, time.time() - start)]
    print >> sys.stderr,  '---- test ----'
    print >> sys.stderr,  'error: %f logreg: %f time: %f' % (1 - correct, cost, time.time() - start)

  def print_net_summary(self):
    print >> sys.stderr,  '--------------------------------------------------------------'
    for s in self.net.get_summary():
      name = s[0]
      values = s[1]
      print >> sys.stderr,  "Layer '%s' weight: %e [%e]" % (name, values[0], values[1])
      print >> sys.stderr,  "Layer '%s' bias: %e [%e]" % (name, values[2], values[3])


  def should_continue_training(self):
    return self.curr_epoch <= self.num_epoch

  def check_test_data(self):
    return self.curr_batch % self.test_freq == 0

  def check_save_checkpoint(self):
    return self.curr_batch % self.save_freq == 0

  def check_adjust_lr(self):
    return self.factor != 1 and self.curr_batch % self.adjust_freq == 0

  def _finished_training(self):
    if self.train_dumper is not None:
      self.train_dumper.flush()

    if self.test_dumper is not None:
      self.test_dumper.flush()

  def should_capture_training_data(self):
    return self.curr_epoch == self.num_epoch

  def _capture_training_data(self):
    if not self.train_dumper:
      return

    self.train_dumper.add({'labels' : self.net.label.get(),
                           'fc' : self.net.outputs[-3].get().transpose() })

  def _capture_test_data(self):
    if not self.test_dumper:
      return
    self.test_dumper.add({'labels' : self.net.label.get(),
                           'fc' : self.net.outputs[-3].get().transpose() })

  def train(self):
    self.print_net_summary()
    util.log('Starting training...')
    while self.should_continue_training():
      train_data = self.train_dp.get_next_batch(self.batch_size)

      self.curr_epoch = train_data.epoch
      self.curr_batch += 1

      start = time.time()
      input, label = train_data.data, train_data.labels
      self.net.train_batch(input, label)
      if self.should_capture_training_data():
        self._capture_training_data()

      cost , correct, numCase = self.net.get_batch_information()
      self.train_outputs += [({'logprob': [cost, 1 - correct]}, numCase, time.time() - start)]
      print >> sys.stderr,  '%d.%d: error: %f logreg: %f time: %f' % (self.curr_epoch, self.curr_batch, 1 - correct, cost, time.time() - start)

      if self.check_test_data():
        self.get_test_error()

      if self.factor != 1.0 and self.check_adjust_lr():
        self.adjust_lr()

      if self.check_save_checkpoint():
        self.save_checkpoint()

    self.get_test_error()
    self.save_checkpoint()
    self.report()
    self._finished_training()

  def predict(self, save_layers = None, filename = None):
    self.net.save_layerouput(save_layers)
    self.print_net_summary()
    util.log('Starting predict...')
    save_output = []
    while self.curr_epoch < 2:
      start = time.time()
      test_data = self.test_dp.get_next_batch(self.batch_size)

      input, label = test_data.data, test_data.labels
      self.net.train_batch(input, label, TEST)
      cost , correct, numCase = self.net.get_batch_information()
      self.curr_epoch = self.test_data.epoch
      self.curr_batch += 1
      print >> sys.stderr,  '%d.%d: error: %f logreg: %f time: %f' % (self.curr_epoch, self.curr_batch, 1 - correct, cost, time.time() - start)
      if save_layers is not None:
        save_output.extend(self.net.get_save_output())

    if save_layers is not None:
      if filename is not None:
        with open(filename, 'w') as f:
          cPickle.dump(save_output, f, protocol = -1)
        util.log('save layer output finished')


  def report(self):
    rep = self.net.get_report()
    if rep is not None:
      print rep
    #timer.report()

  @staticmethod
  def get_trainer_by_name(name, param_dict):
    net = FastNet(param_dict['learning_rate'], param_dict['image_shape'], init_model = None)
    param_dict['net'] = net
    if name == 'layerwise':
      return ImageNetLayerwisedTrainer(**param_dict)

    if name == 'catewise':
      return ImageNetCatewisedTrainer(**param_dict)

    if name == 'categroup':
      return ImageNetCateGroupTrainer(**param_dict)


    net = FastNet(param_dict['learning_rate'], param_dict['image_shape'], param_dict['init_model'])
    param_dict['net'] = net
    if name == 'normal':
      return Trainer(**param_dict)

    if name == 'minibatch':
      return MiniBatchTrainer(**param_dict)

    raise Exception, 'No trainer found for name: %s' % name




class MiniBatchTrainer(Trainer):
  def _finish_init(self):
    self.num_epoch = 100000

  def should_continue_training(self):
    return self.curr_batch < self.num_batch


class AutoStopTrainer(Trainer):
  def _finish_init(self):
    auto_stop_alg = getattr(self, 'auto_stop_alg', 'smooth')
    self.scheduler = Scheduler.makeScheduler(auto_stop_alg, self)

  def should_continue_training(self):
    return Trainer.should_continue_training(self) and self.scheduler.should_continue_training()

  def check_save_checkpoint(self):
    return Trainer.check_save_checkpoint(self) and self.scheduler.check_save_checkpoint()

# don't use recently
#class AdaptiveLearningRateTrainer(Trainer):
#  def __init__(self, test_id, data_dir, provider, checkpoint_dir, train_range, test_range, test_freq,
#      save_freq, batch_size, num_epoch, image_size, image_color, learning_rate, init_model= None, adjust_freq=10, factor=[1.0]):
#    Trainer.__init__(self, test_id, data_dir, provider, checkpoint_dir, train_range, test_range, test_freq,
#        save_freq, batch_size, num_epoch, image_size, image_color, learning_rate,  adjust_freq = adjust_freq,
#        init_model = None, factor=factor)
#    self.train_data = self.train_dp.get_next_batch()
#    batch = self.train_data.batchnum
#
#    # if self.train_data.data.shape[1] > 1000:
#    #  train_data = (self.train_data.data[:, :1000] , self.train_data.labels[:1000])
#    # else:
#    #  train_data = self.train_data
#
#    train_data = self.get_next_minibatch(0)
#    self.train_dp.del_batch(batch)
#
#    _, batch, self.test_data = self.test_dp.get_next_batch()
#    # if self.test_data['data'].shape[1] > 1000:
#    #  test_data = (self.test_data['data'][:, :1000], self.train_data.labels[:1000])
#    # else:
#    #  test_data = self.test_data
#    test_data = self.get_next_minibatch(0, TEST)
#    self.test_dp.del_batch(batch)
#
#    # test_data = self.get_next_minibatch(0)
#    # test_data = train_data
#
#    # train_data= self.train_data
#    # test_data = self.test_data
#    self.net = AdaptiveFastNet(self.learning_rate, self.image_shape, self.n_out, train_data,
#        test_data, init_model = init_model)
#
#  def report(self):
#    lis = self.net.get_report()
#    print 'Iteration:', self.adjust_freq
#    print 'learningRare'
#    for l in lis:
#      print l[0]
#



class ImageNetLayerwisedTrainer(Trainer):
  def _finish_init(self):
    self.curr_model = []
    self.complete_model = self.init_model
    self.fc_params = []
    self.conv_params = []
    self.final_num_epoch = self.num_epoch

    conv = True
    for ld in self.init_model:
      if ld['type'] in ['conv', 'rnorm', 'pool', 'neuron'] and conv:
        #self.conv_params.append(ld)
        self.curr_model.append(ld)
      elif ld['type'] == 'fc' or (not conv and ld['type'] == 'neuron'):
        self.fc_params.append(ld)
        conv = False
      else:
        self.softmax_param = ld

    #self.conv_stack = FastNet.split_conv_to_stack(self.conv_params)
    #for i in range(3):
    #  self.curr_model.extend(self.conv_stack[i])

    self.fc_stack = FastNet.split_fc_to_stack(self.fc_params)
    #tmp = self.conv_stack[3:]
    #tmp.extend(self.fc_stack)
    #self.stack = tmp
    self.stack = self.fc_stack

    self.curr_model.append(self.stack[-1][0])
    self.curr_model.append(self.softmax_param)
    del self.stack[-1]
    pprint.pprint(self.stack)

    self.layerwised = True
    self.num_epoch = 1
    self.net = FastNet(self.learning_rate, self.image_shape, self.curr_model)

  def report(self):
    pass

  def should_continue_training(self):
    #if self.layerwised and self.curr_epoch == 2:
    #  self.net.enable_bprop()
    return self.curr_epoch <= self.num_epoch

  def init_subnet_data_provider(self):
    dp = DataProvider.get_by_name('intermediate')
    count = self.train_dumper.get_count()
    self.train_dp = dp(self.train_output_filename,  range(0, count), 'fc')
    count = self.test_dumper.get_count()
    self.test_dp = dp(self.test_output_filename, range(0, count), 'fc')
    #dp = DataProvider.get_by_name('memory')
    #self.train_dp = dp(self.train_dumper)
    #self.test_dp = dp(self.test_dumper)

  def train(self):
    Trainer.train(self)
    for i, stack in enumerate(self.stack):
      pprint.pprint(stack)
      self.curr_model = self.checkpoint_dumper.get_checkpoint()
      self.curr_batch = self.curr_epoch =  0

      l = self.curr_model['model_state']['layers'][-2]
      assert l['type'] == 'fc'

      l['weight'] = None
      l['bias'] = None
      l['weightIncr'] = None
      l['biasIncr'] = None

      if i == len(self.stack) - 1:
        self.num_epoch = self.final_num_epoch

      layers = self.curr_model['model_state']['layers']
      stack[0]['epsW'] *= self.learning_rate
      stack[0]['epsB'] *= self.learning_rate
      model = [stack[0], stack[1], layers[-2], layers[-1]]

      train_dp_old = self.train_dp
      test_dp_old = self.test_dp
      self.init_subnet_data_provider()

      self.train_dumper = None
      self.test_dumper = None

      image_shape_old = self.image_shape
      shape = self.curr_model['model_state']['layers'][-3]['outputShape']
      size= shape[0] * shape[1] * shape[2]
      self.image_shape = (size, 1, 1, self.batch_size)
      self.net = FastNet(1.0, self.image_shape, init_model = model)

      old_num_epoch = self.num_epoch
      self.num_epoch = 1
      Trainer.train(self)

      self.curr_batch = self.curr_epoch = 0

      self.num_epoch = old_num_epoch

      self.image_shape = image_shape_old
      del layers[-1], layers[-1]
      layers.extend(self.net.get_dumped_layers())

      self.train_dp = train_dp_old
      self.test_dp = test_dp_old

      #for layer in self.curr_model['model_state']['layers'][:-2]:
      #  layer['disableBprop'] = True

      #stack[0]['epsW'] *= self.learning_rate
      #stack[0]['epsB'] *= self.learning_rate
      #self.curr_model['model_state']['layers'].insert(-2, stack[0])
      #self.curr_model['model_state']['layers'].insert(-2, stack[1])


      self.init_output_dumper()
      self.init_data_provider()
      self.net = FastNet(self.learning_rate, self.image_shape,  init_model = self.curr_model)
      Trainer.train(self)


class ImageNetCatewisedTrainer(MiniBatchTrainer):
  def _finish_init(self):
    assert len(self.num_caterange_list) == len(self.num_batch) and self.num_caterange_list[-1] == 1000
    self.num_batch_list  = self.num_batch[1:]
    self.num_batch = self.num_batch[0]

    init_output = self.num_caterange_list[0]
    self.num_caterange_list = self.num_caterange_list[1:]

    fc = self.init_model[-2]
    fc['outputSize'] = init_output

    self.learning_rate_list = self.learning_rate[1:]
    self.learning_rate = self.learning_rate[0]

    self.set_category_range(init_output)
    self.net = FastNet(self.learning_rate, self.image_shape, init_model = self.init_model)
    MiniBatchTrainer._finish_init(self)


  def set_category_range(self, r):
    dp = DataProvider.get_by_name(self.data_provider)
    self.train_dp = dp(self.data_dir, self.train_range, category_range = range(r))
    self.test_dp = dp(self.data_dir, self.test_range, category_range = range(r))


  def train(self):
    MiniBatchTrainer.train(self)

    for i, cate in enumerate(self.num_caterange_list):
      self.set_category_range(cate)
      self.curr_batch = self.curr_epoch = 0
      self.num_batch = self.num_batch_list[i]

      model = self.checkpoint_dumper.get_checkpoint()
      layers = model['model_state']['layers']

      fc = layers[-2]
      fc['weight'] = None
      fc['bias'] = None
      fc['weightIncr'] = None
      fc['biasIncr'] = None
      #for l in layers:
      #  if l['type'] == 'fc':
      #    l['weight'] = None
      #    l['bias'] = None
      #    l['weightIncr'] = None
      #    l['biasIncr'] = None

      #fc = layers[-2]
      fc['outputSize'] = cate

      self.learning_rate = self.learning_rate_list[i]
      self.net = FastNet(self.learning_rate, self.image_shape, init_model = model)

      self.net.clear_weight_incr()
      MiniBatchTrainer.train(self)



class ImageNetCateGroupTrainer(MiniBatchTrainer):
  def _finish_init(self):
    self.num_batch_list = self.num_batch[1:]
    self.num_batch = self.num_batch[0]
    self.learning_rate_list = self.learning_rate[1:]
    self.learning_rate = self.learning_rate[0]

    layers = self.init_model
    fc = layers[-2]
    fc['outputSize'] = self.num_group_list[0]
    self.num_group_list = self.num_group_list[1:]

    self.set_num_group(fc['outputSize'])
    self.net = FastNet(self.learning_rate, self.image_shape, init_model = self.init_model)
    MiniBatchTrainer._finish_init(self)

  def set_num_group(self, n):
    dp = DataProvider.get_by_name(self.data_provider)
    self.train_dp = dp(self.data_dir, self.train_range, n)
    self.test_dp = dp(self.data_dir, self.test_range, n)


  def train(self):
    MiniBatchTrainer.train(self)

    for i, group in enumerate(self.num_group_list):
      self.set_num_group(group)
      self.curr_batch = self.curr_epoch = 0
      self.num_batch = self.num_batch_list[i]

      model = self.checkpoint_dumper.get_checkpoint()
      layers = model['model_state']['layers']

      fc = layers[-2]
      fc['outputSize'] = group
      fc['weight'] = None
      fc['bias'] = None
      fc['weightIncr'] = None
      fc['biasIncr'] = None

      self.learning_rate = self.learning_rate_list[i]
      self.net = FastNet(self.learning_rate, self.image_shape, init_model = model)

      self.net.clear_weight_incr()
      MiniBatchTrainer.train(self)





if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('--test_id', help = 'Test Id', default = None, type = int)
  parser.add_argument('--data_dir', help = 'The directory that data stored')
  parser.add_argument('--param_file', help = 'The param_file or checkpoint file')
  parser.add_argument('--data_provider', help = 'The data provider', choices =['cifar10','imagenet', 'imagenetcategroup'])
  parser.add_argument('--train_range', help = 'The range of the train set')
  parser.add_argument('--test_range', help = 'THe range of the test set')
  parser.add_argument('--save_freq', help = 'How often should I save the checkpoint file', default = 100, type = int)
  parser.add_argument('--test_freq', help = 'How often should I test the model', default = 100, type = int)
  parser.add_argument('--adjust_freq', help = 'How often should I adjust the learning rate', default = 100, type = int)
  parser.add_argument('--factor', help = 'The factor used to adjust the learning rate', default ='1.0')
  parser.add_argument('--learning_rate' , help = 'The scale learning rate', default = '0.1')
  parser.add_argument('--batch_size', help = 'The size of batch', default = 128, type = int)
  parser.add_argument('--checkpoint_dir', help = 'The directory to save checkpoint file')

  parser.add_argument('--trainer', help = 'The type of the trainer', default = 'normal', choices =
      ['normal', 'catewise', 'categroup', 'minibatch', 'layerwise'])


  # extra argument
  extra_argument = ['num_group_list', 'num_caterange_list', 'num_epoch', 'num_batch', 'output_dir']
  parser.add_argument('--num_group_list', help = 'The list of the group you want to split the data to')
  parser.add_argument('--num_caterange_list', help = 'The list of category range you want to train')
  parser.add_argument('--num_epoch', help = 'The number of epoch you want to train', default = 30, type = int)
  parser.add_argument('--num_batch', help = 'The number of minibatch you want to train(num*1000)')
  parser.add_argument('--output_dir', help = 'The directory where to dumper input for last fc layer while training', default='')

  args = parser.parse_args()

  for a in [att for att in dir(args) if not att.startswith('__')]:
    if not getattr(args, a) and a not in extra_argument:
      assert False, 'You have to specify a value of %s' % a


  param_dict = {}
  param_dict['image_color'] = 3
  param_dict['test_id'] = args.test_id
  param_dict['data_dir'] = args.data_dir
  param_dict['data_provider'] = args.data_provider
  if args.data_provider.startswith('imagenet'):
    param_dict['image_size'] = 224
  elif args.data_provider.startswith('cifar'):
    param_dict['image_size'] = 32
  else:
    assert False, 'Unknown data_provider %s' % args.data_provider

  param_dict['train_range'] = util.string_to_int_list(args.train_range)
  param_dict['test_range'] = util.string_to_int_list(args.test_range)
  param_dict['save_freq'] = args.save_freq
  param_dict['test_freq'] = args.test_freq
  param_dict['adjust_freq'] = args.adjust_freq
  factor = util.string_to_float_list(args.factor)
  if len(factor) == 1:
    param_dict['factor'] = factor[0]
  else:
    param_dict['factor'] = factor


  learning_rate = util.string_to_float_list(args.learning_rate)
  if len(learning_rate) == 1:
    param_dict['learning_rate'] = learning_rate[0]
  else:
    param_dict['learning_rate'] = learning_rate

  param_dict['batch_size'] = args.batch_size
  param_dict['checkpoint_dir'] = args.checkpoint_dir
  trainer = args.trainer


  #create a checkpoint dumper
  image_shape = (param_dict['image_color'], param_dict['image_size'], param_dict['image_size'], param_dict['batch_size'])
  param_dict['image_shape'] = image_shape
  cp_dumper = CheckpointDumper(param_dict['checkpoint_dir'], param_dict['test_id'])
  param_dict['checkpoint_dumper'] = cp_dumper

  #create the init_model
  init_model = cp_dumper.get_checkpoint()
  if init_model is None:
    init_model = parse_config_file(args.param_file)
  param_dict['init_model'] = init_model

  #create train dataprovider and test dataprovider
  dp_class = DataProvider.get_by_name(param_dict['data_provider'])
  train_dp = dp_class(param_dict['data_dir'], param_dict['train_range'])
  test_dp = dp_class(param_dict['data_dir'], param_dict['test_range'])
  param_dict['train_dp'] = train_dp
  param_dict['test_dp'] = test_dp


  #get all extra information
  param_dict['num_epoch'] = args.num_epoch
  num_batch = util.string_to_int_list(args.num_batch)
  if len(num_batch) == 1:
    param_dict['num_batch'] = num_batch[0]
  else:
    param_dict['num_batch'] = num_batch

  param_dict['num_group_list']  = util.string_to_int_list(args.num_group_list)
  param_dict['num_caterange_list'] = util.string_to_int_list(args.num_caterange_list)
  param_dict['output_dir'] = args.output_dir


  trainer = Trainer.get_trainer_by_name(trainer, param_dict)
  util.log('start to train...')
  trainer.train()
  #trainer.predict(['pool5'], 'image.opt')
