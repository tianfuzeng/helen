import sys
import torch
import os
from tqdm import tqdm
import torch.distributed as dist
import torch.nn as nn
import torch.multiprocessing as mp

from torch.utils.data import DataLoader
from helen.modules.python.models.dataloader import SequenceDataset
from helen.modules.python.TextColor import TextColor
from helen.modules.python.models.ModelHander import ModelHandler
from helen.modules.python.models.test import test
from helen.modules.python.Options import ImageSizeOptions, TrainOptions

os.environ['PYTHONWARNINGS'] = 'ignore:semaphore_tracker:UserWarning'

"""
Train a model and return the model and optimizer trained.

Input:
- A train CSV containing training image set information (usually chr1-18)

Return:
- A trained model
"""
CLASS_WEIGHTS = [1.0, 1.0, 1.0, 1.0, 1.0]


def save_best_model(transducer_model, model_optimizer, hidden_size, layers, epoch,
                    file_name):
    """
    Save the best model
    :param transducer_model: A trained model
    :param model_optimizer: Model optimizer
    :param hidden_size: Number of hidden layers
    :param layers: Number of GRU layers to use
    :param epoch: Epoch/iteration number
    :param file_name: Output file name
    :return:
    """
    if os.path.isfile(file_name):
        os.remove(file_name)
    ModelHandler.save_checkpoint({
        'model_state_dict': transducer_model.state_dict(),
        'model_optimizer': model_optimizer.state_dict(),
        'hidden_size': hidden_size,
        'gru_layers': layers,
        'epochs': epoch,
    }, file_name)
    sys.stderr.write(TextColor.RED + "\nMODEL SAVED SUCCESSFULLY.\n" + TextColor.END)


def train(train_file, test_file, batch_size, epoch_limit, gpu_mode, num_workers, retrain_model,
          retrain_model_path, gru_layers, hidden_size, lr, decay, model_dir, stats_dir, train_mode,
          world_size, rank, device_id):

    if train_mode is True and rank == 0:
        train_loss_logger = open(stats_dir + "train_loss.csv", 'w')
        test_loss_logger = open(stats_dir + "test_loss.csv", 'w')
        confusion_matrix_logger = open(stats_dir + "confusion_matrix.txt", 'w')
    else:
        train_loss_logger = None
        test_loss_logger = None
        confusion_matrix_logger = None

    torch.cuda.set_device(device_id)

    if rank == 0:
        sys.stderr.write(TextColor.PURPLE + 'Loading data\n' + TextColor.END)

    train_data_set = SequenceDataset(train_file)

    train_sampler = torch.utils.data.distributed.DistributedSampler(
        train_data_set,
        num_replicas=world_size,
        rank=rank
    )

    train_loader = torch.utils.data.DataLoader(
        dataset=train_data_set,
        batch_size=batch_size,
        shuffle=False,
        num_workers=0,
        pin_memory=True,
        sampler=train_sampler)

    num_base_classes = ImageSizeOptions.TOTAL_BASE_LABELS
    num_rle_classes = ImageSizeOptions.TOTAL_RLE_LABELS

    if retrain_model is True:
        if os.path.isfile(retrain_model_path) is False:
            sys.stderr.write(TextColor.RED + "ERROR: INVALID PATH TO RETRAIN PATH MODEL --retrain_model_path\n")
            exit(1)
        sys.stderr.write(TextColor.GREEN + "INFO: RETRAIN MODEL LOADING\n" + TextColor.END)
        transducer_model, hidden_size, gru_layers, prev_ite = \
            ModelHandler.load_simple_model(retrain_model_path,
                                           input_channels=ImageSizeOptions.IMAGE_CHANNELS,
                                           image_features=ImageSizeOptions.IMAGE_HEIGHT,
                                           seq_len=ImageSizeOptions.SEQ_LENGTH,
                                           num_base_classes=num_base_classes,
                                           num_rle_classes=num_rle_classes)

        if train_mode is True:
            epoch_limit = prev_ite + epoch_limit

        sys.stderr.write(TextColor.GREEN + "INFO: RETRAIN MODEL LOADED\n" + TextColor.END)
    else:
        transducer_model = ModelHandler.get_new_gru_model(input_channels=ImageSizeOptions.IMAGE_CHANNELS,
                                                          image_features=ImageSizeOptions.IMAGE_HEIGHT,
                                                          gru_layers=gru_layers,
                                                          hidden_size=hidden_size,
                                                          num_base_classes=num_base_classes,
                                                          num_rle_classes=num_rle_classes)
        prev_ite = 0

    param_count = sum(p.numel() for p in transducer_model.parameters() if p.requires_grad)
    if rank == 0:
        sys.stderr.write(TextColor.RED + "INFO: TOTAL TRAINABLE PARAMETERS:\t" + str(param_count) + "\n" + TextColor.END)

    model_optimizer = torch.optim.Adam(transducer_model.parameters(), lr=lr, weight_decay=decay)

    if retrain_model is True:
        sys.stderr.write(TextColor.GREEN + "INFO: OPTIMIZER LOADING\n" + TextColor.END)
        model_optimizer = ModelHandler.load_simple_optimizer(model_optimizer, retrain_model_path, gpu_mode)
        sys.stderr.write(TextColor.GREEN + "INFO: OPTIMIZER LOADED\n" + TextColor.END)

    if gpu_mode:
        transducer_model = transducer_model.to(device_id)
        transducer_model = nn.parallel.DistributedDataParallel(transducer_model, device_ids=[device_id])

    class_weights = torch.Tensor(TrainOptions.CLASS_WEIGHTS)
    # we perform a multi-task classification, so we need two loss functions, each performing a single task
    # criterion base is the loss function for base prediction
    criterion_base = nn.CrossEntropyLoss()
    # criterion rle is the loss function for RLE prediction
    criterion_rle = nn.CrossEntropyLoss(weight=class_weights)

    if gpu_mode is True:
        criterion_base = criterion_base.to(device_id)
        criterion_rle = criterion_rle.to(device_id)

    start_epoch = prev_ite

    # Train the Model
    if rank == 0:
        sys.stderr.write(TextColor.PURPLE + 'Training starting\n' + TextColor.END)
        sys.stderr.write(TextColor.BLUE + 'Start: ' + str(start_epoch + 1) + ' End: ' + str(epoch_limit) + "\n")

    stats = dict()
    stats['loss_epoch'] = []
    stats['accuracy_epoch'] = []

    for epoch in range(start_epoch, epoch_limit, 1):
        total_loss_base = 0
        total_loss_rle = 0
        total_loss = 0
        total_images = 0
        if rank == 0:
            sys.stderr.write(TextColor.BLUE + 'Train epoch: ' + str(epoch + 1) + "\n")
        # make sure the model is in train mode. BN is different in train and eval.

        batch_no = 1
        if rank == 0:
            progress_bar = tqdm(
                total=len(train_loader),
                ncols=100,
                leave=True,
                position=rank,
                desc="Loss: ",
            )
        else:
            progress_bar = None

        transducer_model.train()
        for images, label_base, label_rle in train_loader:
            # convert the tensors to the proper datatypes.
            images = images.type(torch.FloatTensor)
            label_base = label_base.type(torch.LongTensor)
            label_rle = label_rle.type(torch.LongTensor)

            hidden = torch.zeros(images.size(0), 2 * TrainOptions.GRU_LAYERS, TrainOptions.HIDDEN_SIZE)

            if gpu_mode:
                hidden = hidden.to(device_id)
                images = images.to(device_id)
                label_base = label_base.to(device_id)
                label_rle = label_rle.to(device_id)

            for i in range(0, ImageSizeOptions.SEQ_LENGTH, TrainOptions.WINDOW_JUMP):
                model_optimizer.zero_grad()

                if i + TrainOptions.TRAIN_WINDOW > ImageSizeOptions.SEQ_LENGTH:
                    break

                image_chunk = images[:, i:i+TrainOptions.TRAIN_WINDOW]
                label_base_chunk = label_base[:, i:i+TrainOptions.TRAIN_WINDOW]
                label_rle_chunk = label_rle[:, i:i+TrainOptions.TRAIN_WINDOW]

                # get the inference from the model
                output_base, output_rle, hidden = transducer_model(image_chunk, hidden)

                # calculate loss for base prediction
                loss_base = criterion_base(output_base.contiguous().view(-1, num_base_classes),
                                           label_base_chunk.contiguous().view(-1))
                # calculate loss for RLE prediction
                loss_rle = criterion_rle(output_rle.contiguous().view(-1, num_rle_classes),
                                         label_rle_chunk.contiguous().view(-1))

                # sum the losses to have a singlee optimization over multiple tasks
                loss = loss_base + loss_rle

                # backpropagation and weight update
                loss.backward()
                model_optimizer.step()

                # update the loss values
                total_loss += loss.item()
                total_loss_base += loss_base.item()
                total_loss_rle += loss_rle.item()
                total_images += image_chunk.size(0)

                # detach the hidden from the graph as the next chunk will be a new optimization
                hidden = hidden.detach()

            # update the progress bar
            avg_loss = (total_loss / total_images) if total_images else 0

            if train_mode is True and rank == 0:
                train_loss_logger.write(str(epoch + 1) + "," + str(batch_no) + "," + str(avg_loss) + "\n")

            if rank == 0:
                avg_loss = (total_loss / total_images) if total_images else 0
                progress_bar.set_description("Base: " + str(round(total_loss_base, 4)) +
                                             ", RLE: " + str(round(total_loss_rle, 4)) +
                                             ", TOTAL: " + str(round(total_loss, 4)))
                progress_bar.refresh()
                progress_bar.update(1)
                batch_no += 1

        if rank == 0:
            progress_bar.close()
        dist.barrier()

        if rank == 0:
            stats_dictionary = test(test_file, batch_size, gpu_mode, transducer_model, num_workers,
                                    gru_layers, hidden_size, num_base_classes=ImageSizeOptions.TOTAL_BASE_LABELS,
                                    num_rle_classes=ImageSizeOptions.TOTAL_RLE_LABELS)
            stats['loss'] = stats_dictionary['loss']
            stats['accuracy'] = stats_dictionary['accuracy']
            stats['loss_epoch'].append((epoch, stats_dictionary['loss']))
            stats['accuracy_epoch'].append((epoch, stats_dictionary['accuracy']))
        dist.barrier()

        # update the loggers
        if train_mode is True and rank == 0:
            ModelHandler.save_model(transducer_model, model_optimizer,
                                    hidden_size, gru_layers,
                                    epoch, model_dir + "HELEN_epoch_" + str(epoch + 1) + '_checkpoint.pkl')
            sys.stderr.write(TextColor.RED + "\nMODEL SAVED SUCCESSFULLY.\n" + TextColor.END)

            test_loss_logger.write(str(epoch + 1) + "," + str(stats['loss']) + "," + str(stats['accuracy']) + "\n")
            confusion_matrix_logger.write(str(epoch + 1) + "\n" + str(stats_dictionary['base_confusion_matrix']) + "\n")
            train_loss_logger.flush()
            test_loss_logger.flush()
            confusion_matrix_logger.flush()
        elif train_mode is False:
            # this setup is for hyperband
            if epoch + 1 >= 10 and stats['accuracy'] < 98:
                sys.stderr.write(TextColor.PURPLE + 'EARLY STOPPING AS THE MODEL NOT DOING WELL\n' + TextColor.END)
                return transducer_model, model_optimizer, stats

    if rank == 0:
        sys.stderr.write(TextColor.PURPLE + 'Finished training\n' + TextColor.END)

    return transducer_model, model_optimizer, stats


def cleanup():
    dist.destroy_process_group()


def setup(rank, device_ids, args):
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = '12355'

    # initialize the process group
    dist.init_process_group("gloo", rank=rank, world_size=len(device_ids))

    train_file, test_file, batch_size, epochs, gpu_mode, num_workers, retrain_model, \
    retrain_model_path, gru_layers, hidden_size, learning_rate, weight_decay, model_dir, stats_dir, total_callers, \
    train_mode = args

    # issue with semaphore lock: https://github.com/pytorch/pytorch/issues/2517
    # mp.set_start_method('spawn')

    # Explicitly setting seed to make sure that models created in two processes
    # start from same random weights and biases. https://github.com/pytorch/pytorch/issues/2517
    torch.manual_seed(42)
    train(train_file, test_file, batch_size, epochs, gpu_mode, num_workers, retrain_model, retrain_model_path,
          gru_layers, hidden_size, learning_rate, weight_decay, model_dir, stats_dir, train_mode,
          total_callers, rank, device_ids[rank])
    cleanup()


def train_distributed(train_file, test_file, batch_size, epochs, gpu_mode, num_workers, retrain_model,
                      retrain_model_path, gru_layers, hidden_size, learning_rate, weight_decay, model_dir,
                      stats_dir, device_ids, total_callers, train_mode):

    args = (train_file, test_file, batch_size, epochs, gpu_mode, num_workers, retrain_model,
            retrain_model_path, gru_layers, hidden_size, learning_rate, weight_decay, model_dir,
            stats_dir, total_callers, train_mode)
    mp.spawn(setup,
             args=(device_ids, args),
             nprocs=len(device_ids),
             join=True)
