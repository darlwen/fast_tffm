import threading, time, random
from py.fm_model import LocalFmModel, DistFmModel
import tensorflow as tf
import os

class _TrainStats:
  pass
def _train(sess, supervisor, worker_num, is_master_worker, need_to_init, model, train_files, weight_files, validation_files, epoch_num, thread_num, model_file, model_path, model_version, output_progress_every_n_examples = 10000):
  with sess as sess:
    if is_master_worker:
      if weight_files != None:
        train_and_weight_files = zip(train_files, weight_files)
      else:
        train_and_weight_files = zip(train_files, ["" for i in range(len(train_files))])
      if need_to_init:
        sess.run(model.init_vars)
      for i in range(epoch_num):
        random.shuffle(train_and_weight_files)
        for data_file, weight_file in train_and_weight_files:
          sess.run(model.file_enqueue_op, feed_dict = {model.epoch_id: i, model.is_training: True, model.data_file: data_file, model.weight_file: weight_file})
        if validation_files != None:
          for validation_file in validation_files:
            sess.run(model.file_enqueue_op, feed_dict = {model.epoch_id: i, model.is_training: False, model.data_file: validation_file, model.weight_file: ""})
      sess.run(model.file_close_queue_op)
    try:
      fid = 0
      while True:
        epoch_id, is_training, data_file, weight_file = sess.run(model.file_dequeue_op)
        train_stats = _TrainStats()
        train_stats.processed_example_num = 0
        train_stats.lock = threading.Lock()
        start_time = time.time()
        print '[Epoch %d] Task: %s; Data File: %s'%(epoch_id, 'Training' if is_training else 'Validation', data_file), '; Weight File: %s .'%weight_file if weight_file != '' else '.'

        def run():
          try:
            while not coord.should_stop() and not (supervisor != None and supervisor.should_stop()):
              if is_training:
                _, loss, example_num = sess.run([model.opt, model.loss, model.example_num], feed_dict = {model.file_id: fid, model.data_file: data_file, model.weight_file: weight_file})
                #print '--DEBUG each MB loss: %.5f; epoch example num: %d;'%(loss, example_num)
                global_loss, global_example_num = model.training_stat[epoch_id].update(sess, loss, example_num)
              else:
                loss, example_num = sess.run([model.loss, model.example_num], feed_dict = {model.file_id: fid, model.data_file: data_file, model.weight_file: weight_file})
                global_loss, global_example_num = model.validation_stat[epoch_id].update(sess, loss, example_num)
              if example_num == 0:
                break
              train_stats.lock.acquire()
              train_stats.processed_example_num += example_num
              if train_stats.processed_example_num % output_progress_every_n_examples < example_num:
                t = time.time() - start_time
                print '-- Ex num: %d; Avg loss: %.5f; Time: %.4f; Speed: %.1f ex/sec.'%(global_example_num, global_loss / global_example_num, t, train_stats.processed_example_num / t)
              train_stats.lock.release()
          except Exception, ex:
            coord.request_stop(ex)
            if supervisor != None:
              supervisor.request_stop(ex)
            raise

        coord = tf.train.Coordinator()
        threads = [threading.Thread(target = run) for i in range(thread_num)]
        for th in threads: th.start()
        coord.join(threads, stop_grace_period_secs=5)
        if is_training:
          global_loss, global_example_num = model.training_stat[epoch_id].eval(sess)
        else:
          global_loss, global_example_num = model.validation_stat[epoch_id].eval(sess)
        print 'Finish Processing. Ex num: %d; Avg loss: %.5f.'%(global_example_num, global_loss / global_example_num)
        fid += 1
    except tf.errors.OutOfRangeError:
      pass
    except Exception, ex:
      if supervisor != None:
        supervisor.request_stop(ex)
      raise

    sess.run(model.incre_finshed_worker_num)
    if is_master_worker:
      print 'Waiting for other workers to finish ...'
      while True:
        finished_worker_num = sess.run(model.finished_worker_num)
        if finished_worker_num == worker_num: break
        time.sleep(1)
      print 'Avg. Loss Summary:'
      for i in range(epoch_num):
        training_loss, training_example_num = model.training_stat[i].eval(sess)
        validation_loss, validation_example_num = model.validation_stat[i].eval(sess)
        print '-- [Epoch %d] Training: %.5f'%(i, training_loss / training_example_num),
        if validation_example_num != 0:
          print '; Validation: %.5f'%(validation_loss / validation_example_num)
        else:
          print
#      model.saver.save(sess, model_file, write_meta_graph = False)
#      print 'Model saved to', model_file
      sess.graph._unsafe_unfinalize()
      legacy_init_op = tf.group(tf.tables_initializer(), name='legacy_init_op')
      output_path = os.path.join(
          tf.compat.as_bytes(model_path),
          tf.compat.as_bytes(str(model_version)))
      builder = tf.saved_model.builder.SavedModelBuilder(output_path)
      builder.add_meta_graph_and_variables(
        sess, [tf.saved_model.tag_constants.SERVING],
        signature_def_map={
            'predict_score':
                model.prediction_signature,
        },
        clear_devices=True,
        legacy_init_op=legacy_init_op)

  #    print(model.vocab_blocks[0])
  #    print(model.vocab_blocks[1])
  #    for i in range(25002):
  #      print sess.run(model.vocab_blocks[0][i])
  #    for i in range(25002):
  #      print sess.run(model.vocab_blocks[1][i])

      sess.graph.finalize()
      builder.save(as_text=True)
      print 'Done exporting!'


def _queue_size(train_files, validation_files, epoch_num):
  qsize = len(train_files)
  if validation_files != None:
    qsize += len(validation_files)
  return qsize * epoch_num

def local_train(train_files, weight_files, validation_files, epoch_num, vocabulary_size, vocabulary_block_num, hash_feature_id, factor_num, init_value_range, loss_type, optimizer, batch_size, factor_lambda, bias_lambda, thread_num, model_file, model_path, model_version):
  model = LocalFmModel(_queue_size(train_files, validation_files, epoch_num), epoch_num, vocabulary_size, vocabulary_block_num, hash_feature_id,factor_num, init_value_range, loss_type, optimizer, batch_size, factor_lambda, bias_lambda)
  _train(tf.Session(), None, 1, True, True, model, train_files, weight_files, validation_files, epoch_num, thread_num, model_file, model_path, model_version)

def dist_train(ps_hosts, worker_hosts, job_name, task_idx, train_files, weight_files, validation_files, epoch_num, vocabulary_size, vocabulary_block_num, hash_feature_id, factor_num, init_value_range, loss_type, optimizer, batch_size, factor_lambda, bias_lambda, thread_num, model_file, model_path, model_version):
  cluster = tf.train.ClusterSpec({'ps': ps_hosts, 'worker': worker_hosts})
  server = tf.train.Server(cluster, job_name = job_name, task_index = task_idx)
  if job_name == 'ps':
    server.join()
  elif job_name == 'worker':
    model = DistFmModel(_queue_size(train_files, validation_files, epoch_num), cluster, task_idx, epoch_num, vocabulary_size, vocabulary_block_num, hash_feature_id, factor_num, init_value_range, loss_type, optimizer, batch_size, factor_lambda, bias_lambda)
    sv = tf.train.Supervisor(is_chief = (task_idx == 0), init_op = model.init_vars)
    _train(sv.managed_session(server.target, config = tf.ConfigProto(log_device_placement=False)), sv, len(worker_hosts), task_idx == 0, False, model, train_files, weight_files, validation_files, epoch_num, thread_num, model_file, model_path, model_version)
  else:
    sys.stderr.write('Invalid Job Name: %s'%job_name)
    raise
