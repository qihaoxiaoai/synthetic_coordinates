import torch, torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import global_add_pool, global_mean_pool, global_max_pool

from ogb.graphproppred.mol_encoder import AtomEncoder, BondEncoder
from .torch_vertex import GENConv
from .torch_nn import norm_layer


class DeeperGCN(torch.nn.Module):
    def __init__(
        self,
        num_tasks,
        num_layers=7,
        dropout=0.2,
        conv_encode_edge=False,
        hidden_channels=256,
        gcn_aggr="softmax",
        learn_t=True,
        t=1.0,
        learn_p=False,
        p=1,
        msg_norm=False,
        learn_msg_scale=False,
        norm="batch",
        mlp_layers=1,
        graph_pooling="mean",
        node_feat_dim=None,
        edge_feat_dim=None,
        mol_data=True,
        mlp_act="relu",
        # embedding options
        # embed the basis globally - once for the whole network
        emb_basis_global=True,
        # embed the basis locally - once at each layer
        emb_basis_local=True,
        # bottleneck between the basis and hidden_channels?
        emb_bottleneck=4,
    ):
        super(DeeperGCN, self).__init__()

        self.num_layers = num_layers
        self.dropout = dropout
        self.conv_encode_edge = conv_encode_edge
        # molhiv dataset
        self.mol_data = mol_data
        self.num_tasks = num_tasks

        self.learn_t = learn_t
        self.learn_p = learn_p
        self.msg_norm = msg_norm

        if self.conv_encode_edge:
            # encode the edges once initially
            self.edge_attr_initial = nn.Identity()
        else:
            # use bondencoder for OGB edges to get the initial edge features
            self.edge_attr_initial = BondEncoder(emb_dim=hidden_channels)
            edge_feat_dim = hidden_channels

        if emb_basis_global:
            if emb_bottleneck:
                if emb_basis_local:
                    # basis->emb_bottleneck
                    self.global_emb_edge_attr = nn.Linear(edge_feat_dim, emb_bottleneck)
                else:
                    # basis->bottleneck->hidden_channels
                    self.global_emb_edge_attr = nn.Sequential(
                        nn.Linear(edge_feat_dim, emb_bottleneck),
                        nn.Linear(emb_bottleneck, hidden_channels),
                    )
            else:
                # basis->hidden_channels
                self.global_emb_edge_attr = nn.Linear(edge_feat_dim, hidden_channels)
        else:
            self.global_emb_edge_attr = nn.Identity()

        self.gcns = torch.nn.ModuleList()
        self.norms = torch.nn.ModuleList()

        for _ in range(self.num_layers):
            gcn = GENConv(
                # in and out dims
                hidden_channels,
                hidden_channels,
                # DeeperGCN params
                aggr=gcn_aggr,
                t=t,
                learn_t=self.learn_t,
                p=p,
                learn_p=self.learn_p,
                msg_norm=self.msg_norm,
                learn_msg_scale=learn_msg_scale,
                edge_feat_dim=edge_feat_dim,
                norm=norm,
                mlp_layers=mlp_layers,
                mlp_act=mlp_act,
                # our params
                emb_basis_global=emb_basis_global,
                emb_basis_local=emb_basis_local,
                emb_bottleneck=emb_bottleneck,
            )
            self.gcns.append(gcn)
            self.norms.append(norm_layer(norm, hidden_channels))

        # molecule data?
        if mol_data:
            self.atom_encoder = AtomEncoder(emb_dim=hidden_channels)
        else:
            self.node_features_encoder = torch.nn.Linear(node_feat_dim, hidden_channels)

        self.pool = {
            "sum": global_add_pool,
            "mean": global_mean_pool,
            "max": global_max_pool,
        }[graph_pooling]

        self.graph_pred_linear = torch.nn.Linear(hidden_channels, self.num_tasks)

    def forward(self, input_batch):
        """
        input_batch: the Batch of data objects
        """
        # initial node embedding
        x = input_batch.x
        if self.mol_data:
            h = self.atom_encoder(x)
        else:
            h = self.node_features_encoder(x.float())

        # inital edge embedding
        edge_emb = self.edge_attr_initial(input_batch.edge_attr)
        # global edge embedding
        edge_emb = self.global_emb_edge_attr(edge_emb)

        # get the initial node features
        h = self.gcns[0](h, input_batch.edge_index, edge_emb)

        for layer in range(1, self.num_layers):
            # norm-relu-dropout-GCN
            # batch/instance/layer norm
            h1 = self.norms[layer - 1](h)
            # relu before GCN, so that layer output can be negative
            h2 = F.relu(h1)
            h2 = F.dropout(h2, p=self.dropout, training=self.training)
            h = self.gcns[layer](h2, input_batch.edge_index, edge_emb) + h

        # last norm layer
        h = self.norms[self.num_layers - 1](h)
        h = F.dropout(h, p=self.dropout, training=self.training)

        h_graph = self.pool(h, input_batch.batch)

        return self.graph_pred_linear(h_graph)
