'''
Graph classification and regression
models:
    DeeperGCN
    SMP
    DimeNet++
variants:
    basic
    basic + distance as edge feature
    linegraph with distance emb
    linegraph with distance, angle embs
'''
# flush print statements immediately to stdout so that we can see them
# in the slurm output
import functools
from icgnn.transforms.misc import Finalize_Dist_Basis
print = functools.partial(print, flush=True)

import uuid
from pathlib import Path

import numpy as np
from tqdm import tqdm

from sacred import Experiment
import seml


import torch
from torch import optim
from torch_geometric.loader import DataLoader
from torch_geometric.transforms import Compose
from torch_geometric.utils import degree

from warmup_scheduler import GradualWarmupScheduler

from icgnn.models.smp.smp import SMP, SMP_LineGraph
from icgnn.models.deepergcn.deepergcn import DeeperGCN
from icgnn.models.deepergcn.deepergcn_linegraph import DeeperGCN_LineGraph
from icgnn.transforms.ppr import Add_Linegraph, Set_PPR_Distance, Remove_Distances, \
                        Set_Linegraph_NodeAttr_Distance, Set_Graph_EdgeAttr_Distance, \
                        Set_Linegraph_EdgeAttr, Set_Linegraph_EdgeAttr_Angle
from icgnn.transforms.rdkit import (
    Set_Distance_Matrix, Set_3DCoord_Distance, Set_2DCoord_Distance,
    Set_Pharm3D_Distance, Set_BoundsMatUpper_Distance, \
    Set_BoundsMatLower_Distance, Set_BoundsMatBoth_Distance, \
    Set_Edge_Dist
)
from icgnn.transforms.ogb import Graph_To_Mol
from icgnn.transforms.zinc import ZINC_Graph_To_Mol
from icgnn.transforms.qm9 import QM9_Graph_To_Mol, RemoveTargets

from icgnn.data_utils.data import get_graphcls_dataset, get_transformed_dataset
from icgnn.train_utils.ogb_graphcls import train_eval_model
from icgnn.train_utils.ogb_utils import get_ogbgcode_encoder, get_vocab_mapping, decode_arr_to_seq
from icgnn.train_utils.evaluators import ZINC_Evaluator, QM9_Evaluator

from ogb.graphproppred import PygGraphPropPredDataset, Evaluator

ex = Experiment()
seml.setup_logger(ex)

@ex.config
def config():
    overwrite = None
    db_collection = None
    if db_collection is not None:
        ex.observers.append(seml.create_mongodb_observer(db_collection,
        overwrite=overwrite))
    lr_schedule = True
    min_lr = 1e-5
    patience = 10
    max_epochs = 300

    learning_rate = 2e-4
    l2_reg = 1e-4
    batch_size = 128
    lr_warmup = False
    # random seed
    seed=0
    # our new features
    add_ppr_dist = False
    add_rdkit_dist = False
    linegraph_dist = False
    linegraph_angle = False
    # use the lowest possible angle/highest possible angle
    # only in combination with add_rdkist_dist=bounds_matrix_both
    linegraph_angle_mode='center_both'
    # type of distance basis
    dist_basis_type = 'gaussian'
    # basis dimension
    dist_basis = 4
    angle_basis = 4
    # multiply the distance/angle embedding with the message
    emb_product = True
    emb_use_both = False
    # embed the angle and distance basis once globally?
    emb_basis_global = True
    # embed the angle and distance basis in each message passing layer?
    emb_basis_local = True
    # if using bottleneck, add an extra linear layer in the message passing
    # like dimenet++ - reduce the embedding to this dimension, then back to
    # hidden_channels
    # if both emb_basis_global and emb_basis_local are True, no extra layer required
    emb_bottleneck = False
    # model params
    num_layers = 12
    dropout = 0.5
    block = 'res+'
    conv_encode_edge = False
    add_virtual_node = False
    hidden_channels = 256
    conv = 'gen'
    gcn_aggr = 'mean'
    learn_t = True
    t = 0.1
    learn_p = False
    p = 1
    msg_norm = False
    learn_msg_scale = False
    norm = 'batch'
    mlp_layers = 1
    graph_pooling = 'mean'
    quick_run = False
    mlp_act = 'relu'
    qm9_target_ndx = None
    max_hours = None
    metric_graph_cutoff = False
    metric_graph_edgeattr = None

def get_deg_hist(train_list):
    # Compute in-degree histogram over training data.
    print('Get degree histogram')

    deg = torch.zeros(10, dtype=torch.long)
    minlen = deg.numel()

    for data in tqdm(train_list):
        try:
            try:
                d = degree(data.edge_index[1], num_nodes=data.x.shape[0], dtype=torch.long)
            except:
                d = degree(data.edge_index_lg[1], num_nodes=data.x_g.shape[1], dtype=torch.long)
            deg += torch.bincount(d, minlength=minlen)
        except:
            print('Invalid graph, skip during degree count')

    return deg

class ComposeCustom(object):
  """Composes several transforms together."""

  def __init__(self, transforms):
    self.transforms = transforms

  def __call__(self, data):
    for t in self.transforms:
      data = t(data)
    return data

@ex.automain
def run(dataset_name,
        model,
        # optim params
        lr_schedule, min_lr, patience,
        max_epochs, learning_rate, l2_reg, batch_size,
        lr_warmup,
        # random seed
        seed,
        # our new features
        add_ppr_dist,
        add_rdkit_dist,
        linegraph_dist,
        linegraph_angle,
        linegraph_angle_mode,
        emb_product,
        emb_use_both,
        emb_basis_global,
        emb_basis_local,
        emb_bottleneck,
        # model params
        mlp_act,
        num_layers, dropout, block,
        conv_encode_edge,
        add_virtual_node,
        hidden_channels, conv,
        gcn_aggr, learn_t, t, learn_p, p,
        msg_norm, learn_msg_scale, norm, mlp_layers,
        graph_pooling,
        quick_run,
        dist_basis, angle_basis,
        dist_basis_type,
        qm9_target_ndx,
        metric_graph_cutoff,
        metric_graph_edgeattr,
        max_hours):

    # set random seed
    torch.manual_seed(seed)
    np.random.seed(seed)

    # get the device
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device("cpu")

    ## all dataset transforms
    transforms = []

    if add_ppr_dist:
        print('(transform) Add PPR Distance to graph')
        transforms.append(Set_PPR_Distance(num_dist_basis=dist_basis, dist_basis_type=dist_basis_type))
    if add_rdkit_dist:
        print('(transform) Graph to RDKit Mol')
        if 'ogb' in dataset_name:
            transforms.append(Graph_To_Mol())
        elif dataset_name == 'ZINC':
            transforms.append(ZINC_Graph_To_Mol())
        elif dataset_name == 'QM9':
            transforms.append(QM9_Graph_To_Mol())

        # create one of each, so that arguments can be provided
        # TODO: better way to do this without creating one transform of each kind?
        mapping = {
            'distance_matrix': Set_Distance_Matrix(),
            '3d_coord': Set_3DCoord_Distance(),
            '2d_coord': Set_2DCoord_Distance(),
            'pharm3d': Set_Pharm3D_Distance(),
            'bounds_matrix_upper': Set_BoundsMatUpper_Distance(),
            'bounds_matrix_lower': Set_BoundsMatLower_Distance(),
            'bounds_matrix_both': Set_BoundsMatBoth_Distance(num_dist_basis=dist_basis,
                                                dist_basis_type=dist_basis_type,
                                                metric_graph_cutoff=metric_graph_cutoff,
                                                metric_graph_edgeattr=metric_graph_edgeattr,
                                                ),
            }
        if add_rdkit_dist in mapping:
            print(f'(transform) Add RDKit distance ({add_rdkit_dist}) to graph')
            dist_transform = mapping[add_rdkit_dist]
            transforms.append(dist_transform)
            print(f'(transform) Set data.edge_dist and data.edge_dist_basis, type: {dist_basis_type}')
            transforms.append(Set_Edge_Dist(num_dist_basis=dist_basis, dist_basis_type=dist_basis_type))
        else:
            raise NotImplementedError

    # if multiple basis computed -> merge all of them
    # eg. PPR, BM, ..
    if add_ppr_dist or add_rdkit_dist:
        transforms.append(Finalize_Dist_Basis())

    # if distance is added, check where to use it
    if add_ppr_dist or add_rdkit_dist:
        if linegraph_dist:
            # add an empty linegraph
            # x_lg and edge_attr_lg will be set later
            print('(transform) Add empty linegraph')
            transforms.append(Add_Linegraph())
            # in the linegraph node attr
            print('(transform) Set data.x_lg <- distance')
            transforms.append(Set_Linegraph_NodeAttr_Distance())
        else:
            # or graph edge attr
            print('(transform) Set data.edge_attr <- distance')
            transforms.append(Set_Graph_EdgeAttr_Distance())

    # if we added the distance, linegraph has been created
    if linegraph_dist:
        # add angle as well?
        if linegraph_angle:
            if add_rdkit_dist and add_rdkit_dist != 'bounds_matrix_both':
                print('Ignoring linegraph_angle_mode, add_rdkist_dist != bounds_matrix_both!')
                linegraph_angle_mode = None

            print(f'(transform) Set data.edge_attr_lg <- angle, mode={linegraph_angle_mode}')
            transforms.append(Set_Linegraph_EdgeAttr_Angle(mode=linegraph_angle_mode,
                                                        num_cos_basis=angle_basis))
        # or constant feature
        else:
            # set empty features in linegraph edges
            print('(transform) Set data.edge_attr_lg <- const')
            transforms.append(Set_Linegraph_EdgeAttr())

    # last transform
    # remove distances and related tensors that are not needed
    transforms.append(Remove_Distances())

    if dataset_name == 'QM9':
        transforms.append(RemoveTargets(keep_ndx=(qm9_target_ndx,)))
    # combine all transforms
    transform = ComposeCustom(transforms)

    ### prepare dataset ###
    # get the dataset
    print(f'Using dataset:\t\t{dataset_name}')

    train_set, val_set, test_set = get_graphcls_dataset(dataset_name, icgnn=False,
                                                        transform=transform,
                                                        quick_run=quick_run)

    # Subset objects
    print('Splits:', train_set, val_set, test_set)

    # variables that might be set later
    # TODO: use a better pattern for this
    node_encoder, arr2seq = None, None

    print('Preparing train list')
    train_list = get_transformed_dataset(train_set)
    first_graph = train_list[0]
    print('First train graph:', first_graph)

    # dataset-specific preparation
    if dataset_name == 'ogbg-ppa':
        # multi class, single task
        num_tasks = train_set.dataset.num_classes
        multi_class = True
    elif dataset_name in ('ogbg-molhiv', 'ogbg-molpcba'):
        # binary, multiple tasks
        num_tasks = train_set.dataset.num_tasks
        multi_class = False
        node_feat_dim = first_graph.x.shape[-1]
    elif dataset_name == 'ogbg-code':
        node_encoder = get_ogbgcode_encoder(train_set.dataset.root, hidden_channels)
        # load the dataset again to get the mapping
        # going through each sample in the train_set is slower
        # TODO: avoid this!
        root = Path(train_set.dataset.root).parent
        tmp_dataset = PygGraphPropPredDataset(root=root, name='ogbg-code')
        split_idx = tmp_dataset.get_idx_split()
        _, idx2vocab = get_vocab_mapping([tmp_dataset.data.y[i] for i in split_idx['train']], 5000)
        arr2seq = lambda arr: decode_arr_to_seq(arr, idx2vocab)

        multi_class = True
        num_tasks = 5
    elif dataset_name == 'ZINC':
        conv_encode_edge = True
        num_tasks = 1
        multi_class = False
        task_type = 'regression'
        eval_metric = 'mae'
        node_feat_dim = first_graph.x.shape[-1]

    elif dataset_name == 'QM9':
        conv_encode_edge = True
        num_tasks = first_graph.y.shape[-1]
        multi_class = False
        task_type = 'regression'
        eval_metric = 'mae'
        node_feat_dim = first_graph.x.shape[-1]

    if 'ogb' in dataset_name:
        task_type, eval_metric = train_set.dataset.task_type, \
                                    train_set.dataset.eval_metric

    print(f'Multi class?: {multi_class}, Num tasks: {num_tasks}')
    print(f'Task type: {task_type}')
    print(f'Eval metric: {eval_metric}')

    # what is the edge attr called?
    attr = 'edge_attr_g' if linegraph_dist else 'edge_attr'
    edge_attr_dim = getattr(first_graph, attr).shape[-1]

    follow = ['x_g'] if linegraph_dist else []

    deg_hist = get_deg_hist(train_list) if conv == 'pna' else None

    print('Preparing val list')
    val_list = get_transformed_dataset(val_set)
    print('Preparing test list')
    test_list = get_transformed_dataset(test_set)
    print(f'Train/val/test: {len(train_list)}, {len(val_list)}, {len(test_list)}')

    loaders = {
        'train': DataLoader(train_list, batch_size=batch_size, shuffle=True,
                              num_workers=4, follow_batch=follow),
        'val': DataLoader(val_list, batch_size=batch_size, shuffle=False,
                            num_workers=4, follow_batch=follow),
        'test': DataLoader(test_list, batch_size=batch_size, shuffle=False,
                             num_workers=4, follow_batch=follow)
    }

    # molecule dataset or something else?
    mol_data = dataset_name in ('ogbg-molhiv', 'ogbg-molpcba')
    print(f'Using a molecule dataset? {mol_data}')

    code_data = dataset_name in ('ogbg-code')
    print(f'Using a code dataset?', code_data)

    ### pick the training loss
    if dataset_name in ('ogbg-ppa', 'ogbg-code'):
        criterion = torch.nn.CrossEntropyLoss()
    elif "classification" in task_type:
        criterion = torch.nn.BCEWithLogitsLoss()
    else:
        criterion = torch.nn.L1Loss()
    print('Criterion:', criterion)
    ### ### ###

    # angle basis is a constant if angle is not computed, dimension is 1
    # angle_basis = 1 if not linegraph_angle else angle_basis

    # get the real basis dims, might have changed
    if linegraph_dist:
        lg_node_basis = first_graph.x_lg.shape[-1]
        lg_edge_basis = first_graph.edge_attr_lg.shape[-1]

    if model == 'deepergcn':
        print('Model: DeeperGCN')
        if linegraph_dist:
            model = DeeperGCN_LineGraph(num_tasks,
                        num_layers, dropout, block,
                        hidden_channels, conv,
                        gcn_aggr,
                        learn_t, t, learn_p, p,
                        msg_norm, learn_msg_scale, norm, mlp_layers,
                        graph_pooling,
                        node_attr_dim=node_feat_dim, edge_attr_dim=edge_attr_dim,
                        mlp_act=mlp_act,
                        lg_node_basis=lg_node_basis, lg_edge_basis=lg_edge_basis,
                        emb_basis_global=emb_basis_global,
                        emb_basis_local=emb_basis_local,
                        emb_bottleneck=emb_bottleneck,
                    )
        else:
            model = DeeperGCN(num_tasks,
                        num_layers, dropout, block, conv_encode_edge,
                        add_virtual_node, hidden_channels, conv,
                        gcn_aggr, learn_t, t, learn_p, p,
                        msg_norm, learn_msg_scale, norm, mlp_layers, graph_pooling,
                        node_feat_dim=node_feat_dim,
                        edge_feat_dim=edge_attr_dim, mol_data=mol_data,
                        code_data=code_data, node_encoder=node_encoder,
                        emb_product=emb_product, mlp_act=mlp_act,
                        emb_use_both=emb_use_both,
                        deg_hist=deg_hist,
                    )
    elif model == 'smp':
        print('Model: Structural Message Passing (SMP)')
        if linegraph_dist:
            model = SMP_LineGraph(num_input_features=node_feat_dim,
                 num_edge_features=edge_attr_dim,
                 num_classes=num_tasks, num_layers=num_layers,
                 hidden=32, residual=False, use_edge_features=True,
                 shared_extractor=True,
                 hidden_final=hidden_channels,
                 use_batch_norm=True, use_x=False, map_x_to_u=True,
                 num_towers=8, simplified=False,
                 lg_node_basis=lg_node_basis, lg_edge_basis=lg_edge_basis,
                 graph_pooling=graph_pooling
                 )
        else:
            # add 1 to node feat dim - the X matrix gets increased internally
            model = SMP(num_input_features=node_feat_dim + 1,
                    num_edge_features=edge_attr_dim,
                    num_classes=num_tasks, num_layers=num_layers,
                    hidden=32, residual=False, use_edge_features=True,
                    shared_extractor=True,
                    hidden_final=hidden_channels,
                    use_batch_norm=True, use_x=False, map_x_to_u=True,
                    num_towers=8, simplified=False)
    else:
        raise NotImplementedError


    model = model.to(device)
    print('Created model')

    optimizer = optim.Adam(model.parameters(), lr=learning_rate, weight_decay=l2_reg)

    if lr_warmup:
        scheduler_warmup = GradualWarmupScheduler(optimizer, multiplier=1,
                                                    total_epoch=10)
        optimizer.zero_grad()
        optimizer.step()
    else:
        scheduler_warmup = None

    scheduler = None
    if lr_schedule:
        print('Using Plateau LR Scheduler')
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, factor=0.5,
                        patience=patience, verbose=True)

    if 'ogb' in dataset_name:
        evaluator = Evaluator(dataset_name)
    elif dataset_name == 'ZINC':
        evaluator = ZINC_Evaluator()
    elif dataset_name == 'QM9':
        evaluator = QM9_Evaluator()

    logdir = Path('runs') / str(uuid.uuid4())
    print(f'Logging to: {logdir}')
    if ex.current_run is not None:
        ex.current_run.info = {'logdir': str(logdir)}

    # train and evaluate
    result = train_eval_model(model, loaders, optimizer, evaluator, max_epochs, device,
                             task_type, eval_metric, criterion, multi_class,
                            warmup=scheduler_warmup,
                            scheduler=scheduler, min_lr=min_lr,
                            max_hours=max_hours, logdir=logdir)

    return result