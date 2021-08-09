"""Implementation of IVDetect."""


import json
import pickle as pkl
from glob import glob
from pathlib import Path

import dgl
import networkx as nx
import pandas as pd
import sastvd as svd
import sastvd.helpers.datasets as svdd
import sastvd.helpers.dl as dl
import sastvd.helpers.glove as svdg
import sastvd.helpers.joern as svdj
import sastvd.helpers.ml as ml
import sastvd.helpers.tokenise as svdt
import torch
import torch.nn as nn
import torch.nn.functional as F
from dgl.dataloading import GraphDataLoader
from dgl.nn import GraphConv
from pandarallel import pandarallel
from torch.nn.utils.rnn import pad_sequence
from tqdm import tqdm

tqdm.pandas()
pandarallel.initialize()


def feature_extraction(filepath):
    """Extract relevant components of IVDetect Code Representation.

    DEBUGGING:
    filepath = "/home/david/Documents/projects/singularity-sastvd/storage/processed/bigvul/before/180189.c"
    filepath = "/home/david/Documents/projects/singularity-sastvd/storage/processed/bigvul/before/178165.c"

    PRINTING:
    svdj.plot_graph_node_edge_df(nodes, svdj.rdg(edges, "ast"), [24], 0)
    svdj.plot_graph_node_edge_df(nodes, svdj.rdg(edges, "reftype"))
    pd.options.display.max_colwidth = 500
    print(subseq.to_markdown(mode="github", index=0))
    print(nametypes.to_markdown(mode="github", index=0))
    print(uedge.to_markdown(mode="github", index=0))

    4/5 COMPARISON:
    Theirs: 31, 22, 13, 10, 6, 29, 25, 23
    Ours  : 40, 30, 19, 14, 7, 38, 33, 31
    Pred  : 40,   , 19, 14, 7, 38, 33, 31
    """
    fphash = str(svd.hashstr(str(filepath)))
    cachefp = svd.get_dir(svd.cache_dir() / "ivdetect_feat_ext") / fphash
    try:
        with open(cachefp, "rb") as f:
            return pkl.load(f)
    except:
        pass

    try:
        nodes, edges = svdj.get_node_edges(filepath)
    except:
        return None

    # 1. Generate tokenised subtoken sequences
    subseq = (
        nodes.sort_values(by="code", key=lambda x: x.str.len(), ascending=False)
        .groupby("lineNumber")
        .head(1)
    )
    subseq = subseq[["lineNumber", "code", "local_type"]].copy()
    subseq.code = subseq.local_type + " " + subseq.code
    subseq = subseq.drop(columns="local_type")
    subseq = subseq[~subseq.eq("").any(1)]
    subseq = subseq[subseq.code != " "]
    subseq.lineNumber = subseq.lineNumber.astype(int)
    subseq = subseq.sort_values("lineNumber")
    subseq.code = subseq.code.apply(svdt.tokenise)
    subseq = subseq.set_index("lineNumber").to_dict()["code"]

    # 2. Line to AST
    ast_edges = svdj.rdg(edges, "ast")
    ast_nodes = svdj.drop_lone_nodes(nodes, ast_edges)
    ast_nodes = ast_nodes[ast_nodes.lineNumber != ""]
    ast_nodes.lineNumber = ast_nodes.lineNumber.astype(int)
    ast_nodes.sort_values("lineNumber")
    ast_nodes["lineidx"] = ast_nodes.groupby("lineNumber").cumcount().values
    ast_edges = ast_edges[ast_edges.line_out == ast_edges.line_in]
    ast_dict = pd.Series(ast_nodes.lineidx.values, index=ast_nodes.id).to_dict()
    ast_edges.innode = ast_edges.innode.map(ast_dict)
    ast_edges.outnode = ast_edges.outnode.map(ast_dict)
    ast_edges = ast_edges.groupby("line_in").agg({"innode": list, "outnode": list})
    ast_edges["edges"] = ast_edges.apply(lambda x: [x.outnode, x.innode], axis=1)
    ast_nodes.code = ast_nodes.code.fillna("").apply(svdt.tokenise)
    ast_nodes = ast_nodes.groupby("lineNumber").agg({"code": list})
    ast = ast_edges.join(ast_nodes, how="inner")
    ast["ast"] = ast.apply(lambda x: [x.code, x.edges], axis=1)
    ast = ast.to_dict()["ast"]

    # 3. Variable names and types
    reftype_edges = svdj.rdg(edges, "reftype")
    reftype_nodes = svdj.drop_lone_nodes(nodes, reftype_edges)
    reftype_nx = nx.Graph()
    reftype_nx.add_edges_from(reftype_edges[["innode", "outnode"]].to_numpy())
    reftype_cc = list(nx.connected_components(reftype_nx))
    varnametypes = list()
    for cc in reftype_cc:
        cc_nodes = reftype_nodes[reftype_nodes.id.isin(cc)]
        var_type = cc_nodes[cc_nodes["_label"] == "TYPE"].name.item()
        for idrow in cc_nodes[cc_nodes["_label"] == "IDENTIFIER"].itertuples():
            varnametypes += [[idrow.lineNumber, var_type, idrow.name]]
    nametypes = pd.DataFrame(varnametypes, columns=["lineNumber", "type", "name"])
    nametypes = nametypes.drop_duplicates().sort_values("lineNumber")
    nametypes.type = nametypes.type.apply(svdt.tokenise)
    nametypes.name = nametypes.name.apply(svdt.tokenise)
    nametypes["nametype"] = nametypes.type + " " + nametypes.name
    nametypes = nametypes.groupby("lineNumber").agg({"nametype": lambda x: " ".join(x)})
    nametypes = nametypes.to_dict()["nametype"]

    # 4/5. Data dependency / Control dependency context
    # Group nodes into statements
    nodesline = nodes[nodes.lineNumber != ""].copy()
    nodesline.lineNumber = nodesline.lineNumber.astype(int)
    nodesline = (
        nodesline.sort_values(by="code", key=lambda x: x.str.len(), ascending=False)
        .groupby("lineNumber")
        .head(1)
    )
    edgesline = edges.copy()
    edgesline.innode = edgesline.line_in
    edgesline.outnode = edgesline.line_out
    nodesline.id = nodesline.lineNumber
    edgesline = svdj.rdg(edgesline, "pdg")
    nodesline = svdj.drop_lone_nodes(nodesline, edgesline)
    # Drop duplicate edges
    edgesline = edgesline.drop_duplicates(subset=["innode", "outnode", "etype"])
    # REACHING DEF to DDG
    edgesline["etype"] = edgesline.apply(
        lambda x: "DDG" if x.etype == "REACHING_DEF" else x.etype, axis=1
    )
    edgesline = edgesline[edgesline.innode.apply(lambda x: isinstance(x, float))]
    edgesline = edgesline[edgesline.outnode.apply(lambda x: isinstance(x, float))]
    edgesline_reverse = edgesline[["innode", "outnode", "etype"]].copy()
    edgesline_reverse.columns = ["outnode", "innode", "etype"]
    uedge = pd.concat([edgesline, edgesline_reverse])
    uedge = uedge[uedge.innode != uedge.outnode]
    uedge = uedge.groupby(["innode", "etype"]).agg({"outnode": set})
    uedge = uedge.reset_index()
    if len(uedge) > 0:
        uedge = uedge.pivot("innode", "etype", "outnode")
        if "DDG" not in uedge.columns:
            uedge["DDG"] = None
        if "CDG" not in uedge.columns:
            uedge["CDG"] = None
        uedge = uedge.reset_index()[["innode", "CDG", "DDG"]]
        uedge.columns = ["lineNumber", "control", "data"]
        uedge.control = uedge.control.apply(
            lambda x: list(x) if isinstance(x, set) else []
        )
        uedge.data = uedge.data.apply(lambda x: list(x) if isinstance(x, set) else [])
        data = uedge.set_index("lineNumber").to_dict()["data"]
        control = uedge.set_index("lineNumber").to_dict()["control"]
    else:
        data = {}
        control = {}

    # Generate PDG
    pdg_nodes = nodesline.copy()
    pdg_nodes = pdg_nodes[["id"]].sort_values("id")
    pdg_nodes["subseq"] = pdg_nodes.id.map(subseq).fillna("")
    pdg_nodes["ast"] = pdg_nodes.id.map(ast).fillna("")
    pdg_nodes["nametypes"] = pdg_nodes.id.map(nametypes).fillna("")
    pdg_nodes["data"] = pdg_nodes.id.map(data)
    pdg_nodes["control"] = pdg_nodes.id.map(control)
    pdg_edges = edgesline.copy()
    pdg_nodes = pdg_nodes.reset_index(drop=True).reset_index()
    pdg_dict = pd.Series(pdg_nodes.index.values, index=pdg_nodes.id).to_dict()
    pdg_edges.innode = pdg_edges.innode.map(pdg_dict)
    pdg_edges.outnode = pdg_edges.outnode.map(pdg_dict)
    pdg_edges = pdg_edges.dropna()
    pdg_edges = (pdg_edges.outnode.tolist(), pdg_edges.innode.tolist())

    # Cache
    with open(cachefp, "wb") as f:
        pkl.dump([pdg_nodes, pdg_edges], f)
    return pdg_nodes, pdg_edges


class BigVulGraphDataset:
    """Represent BigVul as graph dataset."""

    def __init__(self, partition="train", sample=-1):
        """Init class."""
        # Get finished samples
        self.finished = [
            int(Path(i).name.split(".")[0])
            for i in glob(str(svd.processed_dir() / "bigvul/before/*nodes*"))
        ]
        self.df = svdd.bigvul()
        self.df = self.df[self.df.label == partition]
        self.df = self.df[self.df.id.isin(self.finished)]

        # Small sample (for debugging):
        if sample > 0:
            self.df = self.df.sample(sample, random_state=0)

        # Filter out samples with no lineNumber from Joern output
        print("Checking validity...", end="")
        self.df["valid"] = self.df.id.parallel_apply(BigVulGraphDataset.check_validity)
        print("Finished.")
        self.df = self.df[self.df.valid]

        # Get mapping from index to sample ID.
        self.df = self.df.reset_index(drop=True).reset_index()
        self.df = self.df.rename(columns={"index": "idx"})
        self.idx2id = pd.Series(self.df.id.values, index=self.df.idx).to_dict()

        # Load Glove vectors.
        glove_path = svd.processed_dir() / "bigvul/glove/vectors.txt"
        self.emb_dict = svdg.glove_dict(glove_path)

    def itempath(_id):
        """Get itempath path from item id."""
        return svd.processed_dir() / f"bigvul/before/{_id}.c"

    def check_validity(_id):
        """Check whether sample with id=_id has node/edges.

        Example:
        _id = 1320
        with open(str(svd.processed_dir() / f"bigvul/before/{_id}.c") + ".nodes.json", "r") as f:
            nodes = json.load(f)
        """
        valid = 0
        with open(str(BigVulGraphDataset.itempath(_id)) + ".nodes.json", "r") as f:
            nodes = json.load(f)
            lineNums = set()
            for n in nodes:
                if "lineNumber" in n.keys():
                    lineNums.add(n["lineNumber"])
                    if len(lineNums) > 1:
                        valid = 1
                        break
            if valid == 0:
                return False
        with open(str(BigVulGraphDataset.itempath(_id)) + ".edges.json", "r") as f:
            edges = json.load(f)
            edge_set = set([i[2] for i in edges])
            if "REACHING_DEF" not in edge_set and "CDG" not in edge_set:
                return False
            return True

    def get_vuln_indices(self, _id):
        """Obtain vulnerable lines from sample ID."""
        df = self.df[self.df.id == _id]
        removed = df.removed.item()
        return dict([(i, 1) for i in removed])

    def cache_features(self):
        """Save features to disk as cache."""
        itempath = BigVulGraphDataset.itempath
        self.df.id.parallel_apply(lambda x: feature_extraction(itempath(x)))
        # for i in tqdm(range(len(self))):
        #     self[i]

    def __getitem__(self, idx):
        """Override getitem."""
        _id = self.idx2id[idx]
        n, e = feature_extraction(BigVulGraphDataset.itempath(_id))
        n["vuln"] = n.id.map(self.get_vuln_indices(_id)).fillna(0)
        g = dgl.graph(e)
        g.ndata["_LINE"] = torch.Tensor(n["id"].astype(int).to_numpy())
        g.ndata["_VULN"] = torch.Tensor(n["vuln"].astype(int).to_numpy())
        g.ndata["_SAMPLE"] = torch.Tensor([_id] * len(n))
        g = dgl.add_self_loop(g)
        return g

    def __len__(self):
        """Get length of dataset."""
        return len(self.df)

    def item(self, _id):
        """Get item data."""
        n, _ = feature_extraction(BigVulGraphDataset.itempath(_id))
        n.subseq = n.subseq.apply(lambda x: svdg.get_embeddings(x, self.emb_dict, 200))
        n.nametypes = n.nametypes.apply(
            lambda x: svdg.get_embeddings(x, self.emb_dict, 200)
        )
        return n

    def stats(self):
        """Print dataset stats."""
        print(self.df.groupby(["label", "vul"]).count()[["id"]])


class IVDetect(nn.Module):
    """IVDetect model."""

    def __init__(self, input_size, hidden_size, num_layers):
        """Initilisation."""
        super(IVDetect, self).__init__()
        self.dropout = nn.Dropout(p=0)
        self.gru = dl.DynamicRNN(nn.GRU(input_size, hidden_size, num_layers, dropout=0))
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

        self.gcn1 = GraphConv(hidden_size, hidden_size)
        self.gcn2 = GraphConv(hidden_size, 2)

    def forward(self, g, dataset):
        """Forward pass."""
        # Load data from disk on CPU
        nodes = list(
            zip(
                g.ndata["_SAMPLE"].detach().cpu().int().numpy(),
                g.ndata["_LINE"].detach().cpu().int().numpy(),
            )
        )
        subseq_dict = dict()
        subseq_lens_dict = dict()
        for sampleid in set([n[0] for n in nodes]):
            data = dataset.item(sampleid)
            for row in data.itertuples():
                rowss = torch.Tensor(row.subseq)
                if len(row.subseq) == 0:
                    rowss = torch.zeros(1, 200)
                subseq_dict[(sampleid, row.id)] = rowss
                subseq_lens_dict[(sampleid, row.id)] = rowss.shape[0]

        subseq = pad_sequence([subseq_dict[i] for i in nodes], batch_first=True)
        subseq_lens = torch.Tensor([subseq_lens_dict[i] for i in nodes]).long()
        subseq = subseq.to(self.device)

        out, hidden = self.gru(subseq, subseq_lens)
        out = out[range(out.shape[0]), subseq_lens - 1, :]
        out = self.dropout(out)

        # Assign node features to graph
        g.ndata["_FEAT"] = out

        # Pool graph outputs
        h = self.gcn1(g, out)
        h = F.relu(h)
        h = self.gcn2(g, h)
        g.ndata["h"] = h
        return dgl.mean_nodes(g, "h")


# Load data
train_ds = BigVulGraphDataset(partition="train")
val_ds = BigVulGraphDataset(partition="val")
train_dl = GraphDataLoader(train_ds, batch_size=32, drop_last=False, shuffle=True)
val_dl = GraphDataLoader(val_ds, batch_size=256, drop_last=False, shuffle=True)

# Create model
dev = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
model = IVDetect(input_size=200, hidden_size=100, num_layers=2)
model.to(dev)

# Debugging a single sample
batch = next(iter(train_dl))
batch = batch.to(dev)
logits = model(batch, train_ds)

# Optimiser
criterion = nn.CrossEntropyLoss()
optimizer = torch.optim.Adam(model.parameters(), lr=0.0001)

# Train loop
logger = ml.LogWriter(model, "model_gru", max_patience=100, val_every=200)
while True:
    for batch in train_dl:

        # Training
        model.train()
        batch = batch.to(dev)
        logits = model(batch, train_ds)
        labels = dgl.max_nodes(batch, "_VULN").long()
        loss = F.cross_entropy(logits, labels)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # Evaluation
        train_mets = ml.get_metrics_logits(labels, logits)
        val_mets = train_mets
        if logger.log_val():
            model.eval()
            with torch.no_grad():
                val_mets_total = []
                for val_batch in tqdm(val_dl):
                    val_batch = val_batch.to(dev)
                    val_labels = dgl.max_nodes(val_batch, "_VULN").long()
                    val_logits = model(val_batch, val_ds)
                    val_mets = ml.get_metrics_logits(val_labels, val_logits)
                    val_mets_total.append(val_mets)
                val_mets = ml.dict_mean(val_mets_total)
        logger.log(train_mets, val_mets)

    # Early Stopping
    if logger.stop():
        break
    logger.epoch()
