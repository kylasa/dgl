import gc
import logging
import math
import os
import sys
from datetime import timedelta
from timeit import default_timer as timer

import constants

import dgl
import numpy as np
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from convert_partition import create_dgl_object, create_metadata_json
from dataset_utils import get_dataset
from dist_lookup import DistLookupService
from globalids import (
    assign_shuffle_global_nids_edges,
    assign_shuffle_global_nids_nodes,
    lookup_shuffle_global_nids_edges,
)
from gloo_wrapper import allgather_sizes, alltoallv_cpu, gather_metadata_json
from utils import (
    augment_edge_data,
    DATA_TYPE_ID,
    get_edge_types,
    get_etype_featnames,
    get_gid_offsets,
    get_gnid_range_map,
    get_idranges,
    get_node_types,
    get_ntype_counts_map,
    get_ntype_featnames,
    map_partid_rank,
    memory_snapshot,
    read_json,
    read_ntype_partition_files,
    REV_DATA_TYPE_ID,
    write_dgl_objects,
    write_metadata_json,
)


def _get_num_chunks(shapes, dtype_sz, rank, world_size, msg_size):
    """
    Computes the total no. of chunks each process/rank has to split its
    local data during data shuffling phase

    Parameters:
    ----------
    shapes : list
        concatenation of numpy array shapes and each numpy array is
        the shape of feature data on each rank
    dtype_size : integer
        indicating the size of each element in the feature data numpy array
    rank : integer
        rank of the current process
    world_size : integer
        total no. of processes
    msg_size : integer
        max. size of outgoing message
    """
    MB = 1024 * 1024
    assert shapes[0] != 0
    shapes = np.split(shapes, world_size)
    sizes = [np.prod(s) * dtype_sz for s in shapes]

    if msg_size == 0:
        return 1

    max_msg_size = 1 if np.amax(sizes) < MB else np.floor(np.amax(sizes) / MB).astype(np.int32)
    num_chunks = np.ceil(max_msg_size / msg_size).astype(np.int32)
    return num_chunks


def gen_node_data(
    rank, world_size, num_parts, id_lookup, ntid_ntype_map, schema_map
):
    """
    For this data processing pipeline, reading node files is not needed. All the needed information about
    the nodes can be found in the metadata json file. This function generates the nodes owned by a given
    process, using metis partitions.

    Parameters:
    -----------
    rank : int
        rank of the process
    world_size : int
        total no. of processes
    num_parts : int
        total no. of partitions
    id_lookup : instance of class DistLookupService
       Distributed lookup service used to map global-nids to respective partition-ids and
       shuffle-global-nids
    ntid_ntype_map :
        a dictionary where keys are node_type ids(integers) and values are node_type names(strings).
    schema_map:
        dictionary formed by reading the input metadata json file for the input dataset.

        Please note that, it is assumed that for the input graph files, the nodes of a particular node-type are
        split into `p` files (because of `p` partitions to be generated). On a similar node, edges of a particular
        edge-type are split into `p` files as well.

        #assuming m nodetypes present in the input graph
        "num_nodes_per_chunk" : [
            [a0, a1, a2, ... a<p-1>],
            [b0, b1, b2, ... b<p-1>],
            ...
            [m0, m1, m2, ... m<p-1>]
        ]
        Here, each sub-list, corresponding a nodetype in the input graph, has `p` elements. For instance [a0, a1, ... a<p-1>]
        where each element represents the number of nodes which are to be processed by a process during distributed partitioning.

        In addition to the above key-value pair for the nodes in the graph, the node-features are captured in the
        "node_data" key-value pair. In this dictionary the keys will be nodetype names and value will be a dictionary which
        is used to capture all the features present for that particular node-type. This is shown in the following example:

        "node_data" : {
            "paper": {       # node type
                "feat": {   # feature key
                    "format": {"name": "numpy"},
                    "data": ["node_data/paper-feat-part1.npy", "node_data/paper-feat-part2.npy"]
                },
                "label": {   # feature key
                    "format": {"name": "numpy"},
                    "data": ["node_data/paper-label-part1.npy", "node_data/paper-label-part2.npy"]
                },
                "year": {   # feature key
                    "format": {"name": "numpy"},
                    "data": ["node_data/paper-year-part1.npy", "node_data/paper-year-part2.npy"]
                }
            }
        }
        In the above textual description we have a node-type, which is paper, and it has 3 features namely feat, label and year.
        Each feature has `p` files whose location in the filesystem is the list for the key "data" and "foramt" is used to
        describe storage format.

    Returns:
    --------
    dictionary :
        dictionary where keys are column names and values are numpy arrays, these arrays are generated by
        using information present in the metadata json file

    """
    local_node_data = {}
    for local_part_id in range(num_parts // world_size):
        local_node_data[constants.GLOBAL_NID + "/" + str(local_part_id)] = []
        local_node_data[constants.NTYPE_ID + "/" + str(local_part_id)] = []
        local_node_data[
            constants.GLOBAL_TYPE_NID + "/" + str(local_part_id)
        ] = []

    # Note that `get_idranges` always returns two dictionaries. Keys in these
    # dictionaries are type names for nodes and edges and values are
    # `num_parts` number of tuples indicating the range of type-ids in first
    # dictionary and range of global-nids in the second dictionary.
    type_nid_dict, global_nid_dict = get_idranges(
        schema_map[constants.STR_NODE_TYPE],
        get_ntype_counts_map(
            schema_map[constants.STR_NODE_TYPE],
            schema_map[constants.STR_NUM_NODES_PER_TYPE],
        ),
        num_chunks=num_parts,
    )

    for ntype_id, ntype_name in ntid_ntype_map.items():
        # No. of nodes in each process can differ significantly in lopsided distributions
        # Synchronize on a per ntype basis
        dist.barrier()

        type_start, type_end = (
            type_nid_dict[ntype_name][0][0],
            type_nid_dict[ntype_name][-1][1],
        )
        gnid_start, gnid_end = (
            global_nid_dict[ntype_name][0, 0],
            global_nid_dict[ntype_name][0, 1],
        )

        node_partid_slice = id_lookup.get_partition_ids(
            np.arange(gnid_start, gnid_end, dtype=np.int64)
        )  # exclusive

        for local_part_id in range(num_parts // world_size):
            cond = node_partid_slice == (rank + local_part_id * world_size)
            own_gnids = np.arange(gnid_start, gnid_end, dtype=np.int64)
            own_gnids = own_gnids[cond]

            own_tnids = np.arange(type_start, type_end, dtype=np.int64)
            own_tnids = own_tnids[cond]

            local_node_data[
                constants.NTYPE_ID + "/" + str(local_part_id)
            ].append(np.ones(own_gnids.shape, dtype=np.int64) * ntype_id)
            local_node_data[
                constants.GLOBAL_NID + "/" + str(local_part_id)
            ].append(own_gnids)
            local_node_data[
                constants.GLOBAL_TYPE_NID + "/" + str(local_part_id)
            ].append(own_tnids)

    for k in local_node_data.keys():
        local_node_data[k] = np.concatenate(local_node_data[k])

    return local_node_data


def exchange_edge_data(rank, world_size, num_parts, edge_data, id_lookup):
    """
    Exchange edge_data among processes in the world.
    Prepare list of sliced data targeting each process and trigger
    alltoallv_cpu to trigger messaging api

    Parameters:
    -----------
    rank : int
        rank of the process
    world_size : int
        total no. of processes
    edge_data : dictionary
        edge information, as a dicitonary which stores column names as keys and values
        as column data. This information is read from the edges.txt file.
    id_lookup : DistLookupService instance
        this object will be used to retrieve ownership information of nodes

    Returns:
    --------
    dictionary :
        the input argument, edge_data, is updated with the edge data received by other processes
        in the world.
    """

    # Synchronize at the beginning of this function
    dist.barrier()

    # Prepare data for each rank in the cluster.
    start = timer()

    CHUNK_SIZE = 100 * 1000 * 1000  # 100 * 8 * 5 = 1 * 4 = 8 GB/message/node
    num_edges = edge_data[constants.GLOBAL_SRC_ID].shape[0]
    all_counts = allgather_sizes(
        [num_edges], world_size, num_parts, return_sizes=True
    )
    max_edges = np.amax(all_counts)
    all_edges = np.sum(all_counts)
    num_chunks = (max_edges // CHUNK_SIZE) + (
        0 if (max_edges % CHUNK_SIZE == 0) else 1
    )
    LOCAL_CHUNK_SIZE = (num_edges // num_chunks) + (
        0 if (num_edges % num_chunks == 0) else 1
    )
    logging.info(
        f"[Rank: {rank} Edge Data Shuffle - max_edges: {max_edges}, \
                        local_edges: {num_edges} and num_chunks: {num_chunks} \
                        Total edges: {all_edges} Local_CHUNK_SIZE: {LOCAL_CHUNK_SIZE}"
    )

    for local_part_id in range(num_parts // world_size):
        local_src_ids = []
        local_dst_ids = []
        local_type_eids = []
        local_etype_ids = []
        local_eids = []

        for chunk in range(num_chunks):
            start = chunk * LOCAL_CHUNK_SIZE
            end = (chunk + 1) * LOCAL_CHUNK_SIZE

            logging.info(
                f"[Rank: {rank}] EdgeData Shuffle: processing \
                    local_part_id: {local_part_id} and chunkid: {chunk}"
            )
            cur_src_id = edge_data[constants.GLOBAL_SRC_ID][start:end]
            cur_dst_id = edge_data[constants.GLOBAL_DST_ID][start:end]
            cur_type_eid = edge_data[constants.GLOBAL_TYPE_EID][start:end]
            cur_etype_id = edge_data[constants.ETYPE_ID][start:end]
            cur_eid = edge_data[constants.GLOBAL_EID][start:end]

            input_list = []
            owner_ids = id_lookup.get_partition_ids(cur_dst_id)
            for idx in range(world_size):
                send_idx = owner_ids == (idx + local_part_id * world_size)
                send_idx = send_idx.reshape(cur_src_id.shape[0])
                filt_data = np.column_stack(
                    (
                        cur_src_id[send_idx == 1],
                        cur_dst_id[send_idx == 1],
                        cur_type_eid[send_idx == 1],
                        cur_etype_id[send_idx == 1],
                        cur_eid[send_idx == 1],
                    )
                )
                if filt_data.shape[0] <= 0:
                    input_list.append(torch.empty((0, 5), dtype=torch.int64))
                else:
                    input_list.append(torch.from_numpy(filt_data))

            # Now send newly formed chunk to others.
            dist.barrier()
            output_list = alltoallv_cpu(
                rank, world_size, input_list, retain_nones=False
            )

            # Replace the values of the edge_data, with the received data from all the other processes.
            rcvd_edge_data = torch.cat(output_list).numpy()
            local_src_ids.append(rcvd_edge_data[:, 0])
            local_dst_ids.append(rcvd_edge_data[:, 1])
            local_type_eids.append(rcvd_edge_data[:, 2])
            local_etype_ids.append(rcvd_edge_data[:, 3])
            local_eids.append(rcvd_edge_data[:, 4])

        edge_data[
            constants.GLOBAL_SRC_ID + "/" + str(local_part_id)
        ] = np.concatenate(local_src_ids)
        edge_data[
            constants.GLOBAL_DST_ID + "/" + str(local_part_id)
        ] = np.concatenate(local_dst_ids)
        edge_data[
            constants.GLOBAL_TYPE_EID + "/" + str(local_part_id)
        ] = np.concatenate(local_type_eids)
        edge_data[
            constants.ETYPE_ID + "/" + str(local_part_id)
        ] = np.concatenate(local_etype_ids)
        edge_data[
            constants.GLOBAL_EID + "/" + str(local_part_id)
        ] = np.concatenate(local_eids)

    # Check if the data was exchanged correctly
    local_edge_count = 0
    for local_part_id in range(num_parts // world_size):
        local_edge_count += edge_data[
            constants.GLOBAL_SRC_ID + "/" + str(local_part_id)
        ].shape[0]
    shuffle_edge_counts = allgather_sizes(
        [local_edge_count], world_size, num_parts, return_sizes=True
    )
    shuffle_edge_total = np.sum(shuffle_edge_counts)
    assert shuffle_edge_total == all_edges

    end = timer()
    logging.info(
        f"[Rank: {rank}] Time to send/rcv edge data: {timedelta(seconds=end-start)}"
    )

    # Clean up.
    edge_data.pop(constants.GLOBAL_SRC_ID)
    edge_data.pop(constants.GLOBAL_DST_ID)
    edge_data.pop(constants.GLOBAL_TYPE_EID)
    edge_data.pop(constants.ETYPE_ID)
    edge_data.pop(constants.GLOBAL_EID)

    return edge_data


def exchange_feature(
    rank,
    data,
    id_lookup,
    feat_type,
    feat_key,
    featdata_key,
    gid_start,
    gid_end,
    type_id_start,
    type_id_end,
    local_part_id,
    world_size,
    num_parts,
    cur_features,
    cur_global_ids,
):
    """This function is used to send/receive one feature for either nodes or
    edges of the input graph dataset.

    Parameters:
    -----------
    rank : int
        integer, unique id assigned to the current process
    data: dicitonary
        dictionry in which node or edge features are stored and this information
        is read from the appropriate node features file which belongs to the
        current process
    id_lookup : instance of DistLookupService
        instance of an implementation of dist. lookup service to retrieve values
        for keys
    feat_type : string
        this is used to distinguish which features are being exchanged. Please
        note that for nodes ownership is clearly defined and for edges it is
        always assumed that destination end point of the edge defines the
        ownership of that particular edge
    feat_key : string
        this string is used as a key in the dictionary to store features, as
        tensors, in local dictionaries
    featdata_key : numpy array
        features associated with this feature key being processed
    gid_start : int
        starting global_id, of either node or edge, for the feature data
    gid_end : int
        ending global_if, of either node or edge, for the feature data
    type_id_start : int
        starting type_id for the feature data
    type_id_end : int
        ending type_id for the feature data
    local_part_id : int
        integers used to the identify the local partition id used to locate
        data belonging to this partition
    world_size : int
        total number of processes created
    num_parts : int
        total number of partitions
    cur_features : dictionary
        dictionary to store the feature data which belongs to the current
        process
    cur_global_ids : dictionary
        dictionary to store global ids, of either nodes or edges, for which
        the features stored in the cur_features dictionary

    Returns:
    -------
    dictionary :
        a dictionary is returned where keys are type names and
        feature data are the values
    list :
        a dictionary of global_ids either nodes or edges whose features are
        received during the data shuffle process
    """
    # type_ids for this feature subset on the current rank
    gids_feat = np.arange(gid_start, gid_end)
    tids_feat = np.arange(type_id_start, type_id_end)
    local_idx = np.arange(0, type_id_end - type_id_start)

    feats_per_rank = []
    global_id_per_rank = []

    tokens = feat_key.split("/")
    assert len(tokens) == 3
    local_feat_key = "/".join(tokens[:-1]) + "/" + str(local_part_id)

    logging.info(
        f"[Rank: {rank} feature: {feat_key}, gid_start - {gid_start} and gid_end - {gid_end}"
    )

    # Get the partition ids for the range of global nids.
    if feat_type == constants.STR_NODE_FEATURES:
        # Retrieve the partition ids for the node features.
        # Each partition id will be in the range [0, num_parts).
        partid_slice = id_lookup.get_partition_ids(
            np.arange(gid_start, gid_end, dtype=np.int64)
        )
    else:
        # Edge data case.
        # Ownership is determined by the destination node.
        assert data is not None
        global_eids = np.arange(gid_start, gid_end, dtype=np.int64)
        if data[constants.GLOBAL_EID].shape[0] > 0:
            logging.info(
                f"[Rank: {rank} disk read global eids - min - {np.amin(data[constants.GLOBAL_EID])}, max - {np.amax(data[constants.GLOBAL_EID])}, count - {data[constants.GLOBAL_EID].shape}"
            )

        # Now use `data` to extract destination nodes' global id
        # and use that to get the ownership
        common, idx1, idx2 = np.intersect1d(
            data[constants.GLOBAL_EID], global_eids, return_indices=True
        )
        assert common.shape[0] == idx2.shape[0]
        assert common.shape[0] == global_eids.shape[0]

        global_dst_nids = data[constants.GLOBAL_DST_ID][idx1]
        assert np.all(global_eids == data[constants.GLOBAL_EID][idx1])
        partid_slice = id_lookup.get_partition_ids(global_dst_nids)

    # determine the shape of the feature-data
    # this is needed to so that ranks where feature-data is not present
    # should use the correct shape for sending the padded vector.
    # exchange length here.
    feat_dim_len = 0
    if featdata_key is not None:
        feat_dim_len = len(featdata_key.shape)
    all_lens = allgather_sizes(
        [feat_dim_len], world_size, num_parts, return_sizes=True
    )
    if all_lens[0] <= 0:
        logging.info(
            f"[Rank: {rank} No process has any feature data to shuffle for {local_feat_key}"
        )
        return cur_features, cur_global_ids

    rank0_shape_len = all_lens[0]
    for idx in range(1, world_size):
        assert (all_lens[idx] == 0) or (all_lens[idx] == rank0_shape_len), (
            f"feature: {local_feat_key} shapes does not match "
            f"at rank - {idx} and rank - 0"
        )

    # exchange actual data here.
    if featdata_key != None:
        feat_dims_dtype = list(featdata_key.shape)
        feat_dims_dtype.append(DATA_TYPE_ID[featdata_key.dtype])
    else:
        feat_dims_dtype = list(np.zeros((rank0_shape_len), dtype=np.int64))
        feat_dims_dtype.append(DATA_TYPE_ID[torch.float32])

    logging.info(f"Sending the feature shape information - {feat_dims_dtype}")
    all_dims_dtype = allgather_sizes(
        feat_dims_dtype, world_size, num_parts, return_sizes=True
    )

    for idx in range(world_size):
        cond = partid_slice == (idx + local_part_id * world_size)
        gids_per_partid = gids_feat[cond]
        tids_per_partid = tids_feat[cond]
        local_idx_partid = local_idx[cond]

        if gids_per_partid.shape[0] == 0:
            assert len(all_dims_dtype) % world_size == 0
            dim_len = int(len(all_dims_dtype) / world_size)
            rank0_shape = tuple(list(np.zeros((dim_len - 1), dtype=np.int32)))
            rank0_dtype = REV_DATA_TYPE_ID[
                all_dims_dtype[(dim_len - 1) : (dim_len)][0]
            ]
            data = torch.empty(rank0_shape, dtype=rank0_dtype)
            feats_per_rank.append(data)
            global_id_per_rank.append(torch.empty((0,), dtype=torch.int64))
        else:
            feats_per_rank.append(featdata_key[local_idx_partid])
            global_id_per_rank.append(
                torch.from_numpy(gids_per_partid).type(torch.int64)
            )
    for idx, tt in enumerate(feats_per_rank):
        logging.info(
            f"[Rank: {rank} features shape - {tt.shape} and ids - {global_id_per_rank[idx].shape}"
        )

    # features (and global nids) per rank to be sent out are ready
    # for transmission, perform alltoallv here.
    output_feat_list = alltoallv_cpu(
        rank, world_size, feats_per_rank, retain_nones=False
    )
    output_id_list = alltoallv_cpu(
        rank, world_size, global_id_per_rank, retain_nones=False
    )
    logging.info(
        f"[Rank : {rank} feats - {output_feat_list}, ids - {output_id_list}"
    )
    assert len(output_feat_list) == len(output_id_list), (
        "Length of feature list and id list are expected to be equal while "
        f"got {len(output_feat_list)} and {len(output_id_list)}."
    )

    # stitch node_features together to form one large feature tensor
    if len(output_feat_list) > 0:
        output_feat_list = torch.cat(output_feat_list)
        output_id_list = torch.cat(output_id_list)
        if local_feat_key in cur_features:
            temp = cur_features[local_feat_key]
            cur_features[local_feat_key] = torch.cat([temp, output_feat_list])
            temp = cur_global_ids[local_feat_key]
            cur_global_ids[local_feat_key] = torch.cat([temp, output_id_list])
        else:
            cur_features[local_feat_key] = output_feat_list
            cur_global_ids[local_feat_key] = output_id_list

    return cur_features, cur_global_ids


def exchange_features(
    rank,
    world_size,
    num_parts,
    feat_mesg_size,
    feature_tids,
    type_id_map,
    id_lookup,
    feature_data,
    feat_type,
    data,
):
    """
    This function is used to shuffle node features so that each process will receive
    all the node features whose corresponding nodes are owned by the same process.
    The mapping procedure to identify the owner process is not straight forward. The
    following steps are used to identify the owner processes for the locally read node-
    features.
    a. Compute the global_nids for the locally read node features. Here metadata json file
        is used to identify the corresponding global_nids. Please note that initial graph input
        nodes.txt files are sorted based on node_types.
    b. Using global_nids and metis partitions owner processes can be easily identified.
    c. Now each process sends the global_nids for which shuffle_global_nids are needed to be
        retrieved.
    d. After receiving the corresponding shuffle_global_nids these ids are added to the
        node_data and edge_data dictionaries

    This pipeline assumes all the input data in numpy format, except node/edge features which
    are maintained as tensors throughout the various stages of the pipeline execution.

    Parameters:
    -----------
    rank : int
        rank of the current process
    world_size : int
        total no. of participating processes.
    num_parts : int
        no. of output graph partitions
    feat_mesg_size : int
        maximum size of the outgoing message in MBs. Default value is 0.
    feature_tids : dictionary
        dictionary with keys as node-type names with suffixes as feature names
        and value is a dictionary. This dictionary contains information about
        node-features associated with a given node-type and value is a list.
        This list contains a of indexes, like [starting-idx, ending-idx) which
        can be used to index into the node feature tensors read from
        corresponding input files.
    type_id_map : dictionary
        mapping between type names and global_ids, of either nodes or edges,
        which belong to the keys in this dictionary
    id_lookup : instance of class DistLookupService
       Distributed lookup service used to map global-nids to respective
       partition-ids and shuffle-global-nids
    feature_data : dict
        where keys are feature names and values are numpy arrays as features
    feat_type : string
        this is used to distinguish which features are being exchanged. Please
        note that for nodes ownership is clearly defined and for edges it is
        always assumed that destination end point of the edge defines the
        ownership of that particular edge
    data: dicitonary
        dictionry in which node or edge features are stored and this information
        is read from the appropriate node features file which belongs to the
        current process

    Returns:
    --------
    dictionary :
        a dictionary is returned where keys are type names and
        feature data are the values
    list :
        a dictionary of global_ids either nodes or edges whose features are
        received during the data shuffle process
    """
    start = timer()
    own_features = {}
    own_global_ids = {}

    # To iterate over the node_types and associated node_features
    for feat_key, type_info in feature_tids.items():
        # To iterate over the feature data, of a given (node or edge )type
        # type_info is a list of 3 elements (as shown below):
        #   [feature-name, starting-idx, ending-idx]
        #       feature-name is the name given to the feature-data,
        #       read from the input metadata file
        #       [starting-idx, ending-idx) specifies the range of indexes
        #        associated with the features data
        # Determine the owner process for these features.
        # Note that the keys in the node features (and similarly edge features)
        # dictionary is of the following format:
        #   `node_type/feature_name/local_part_id`:
        #    where node_type and feature_name are self-explanatory and
        #    local_part_id denotes the partition-id, in the local process,
        #    which will be used a suffix to store all the information of a
        #    given partition which is processed by the current process. Its
        #    values start from 0 onwards, for instance 0, 1, 2 ... etc.
        #    local_part_id can be easily mapped to global partition id very
        #    easily, using cyclic ordering. All local_part_ids = 0 from all
        #    processes will form global partition-ids between 0 and world_size-1.
        #    Similarly all local_part_ids = 1 from all processes will form
        #    global partition ids in the range [world_size, 2*world_size-1] and
        #    so on.
        tokens = feat_key.split("/")
        assert len(tokens) == 3
        type_name = tokens[0]
        feat_name = tokens[1]
        logging.info(f"[Rank: {rank}] processing feature: {feat_key}, msg_size = {feat_mesg_size}")

        for feat_info in type_info:
            # Compute the global_id range for this feature data
            type_id_start = int(feat_info[0])
            type_id_end = int(feat_info[1])
            begin_global_id = type_id_map[type_name][0]
            gid_start = begin_global_id + type_id_start
            gid_end = begin_global_id + type_id_end

            # Check if features exist for this type_name + feat_name.
            # This check should always pass, because feature_tids are built
            # by reading the input metadata json file for existing features.
            assert feat_key in feature_data

            for local_part_id in range(num_parts // world_size):
                featdata_key = feature_data[feat_key]

                # Synchronize for each feature
                dist.barrier()

                # Get the shape of feature data, from everyone
                # determine the maximum size on each node.
                # determine the no. of chunks needed to shuffle this feature
                feat_dims = [0]
                if featdata_key != None:
                    feat_dims = featdata_key.shape
                all_dims = allgather_sizes(
                    feat_dims, world_size, num_parts, return_sizes=True
                )
                if all_dims[0] <= 0:
                    logging.info(
                        f"[Rank: {rank} No process has any feature data to shuffle for {local_feat_key}"
                    )
                    continue

                num_msg_chunks = _get_num_chunks(
                    all_dims,
                    featdata_key.element_size(),
                    rank,
                    world_size,
                    feat_mesg_size,
                )

                start = 0
                end = 0
                rows = featdata_key.shape[0]
                rows_per_chunk = rows if rows < num_msg_chunks else np.floor(rows/num_msg_chunks).astype(np.int32)
                num_msg_chunks = np.ceil(rows/rows_per_chunk).astype(np.int32)

                chunk_typeid_start = type_id_start
                chunk_typeid_end = chunk_typeid_start

                chunk_gid_start = gid_start
                chunk_gid_end = chunk_gid_start
                for _ in range(num_msg_chunks):
                    end += rows_per_chunk
                    chunk_typeid_end += rows_per_chunk
                    chunk_gid_end += rows_per_chunk
                    if end > rows:
                        end = rows
                        chunk_typeid_end = type_id_end
                        chunk_gid_end = gid_end

                    logging.info(f"[Rank: {rank} start={start}, end={end}")
                    chunk_feat_data = featdata_key[
                        start:end,
                    ]
                    own_features, own_global_ids = exchange_feature(
                        rank,
                        data,
                        id_lookup,
                        feat_type,
                        feat_key,
                        chunk_feat_data,
                        chunk_gid_start,
                        chunk_gid_end,
                        chunk_typeid_start,
                        chunk_typeid_end,
                        local_part_id,
                        world_size,
                        num_parts,
                        own_features,
                        own_global_ids,
                    )
                    start = end
                    chunk_typeid_start = chunk_typeid_end
                    chunk_gid_start = chunk_gid_end

    end = timer()
    logging.info(
        f"[Rank: {rank}] Total time for feature exchange: {timedelta(seconds = end - start)}"
    )
    for k, v in own_features.items():
        logging.info(f"Rank: {rank}] Key - {k} Value - {v.shape}")
    return own_features, own_global_ids


def exchange_graph_data(
    rank,
    world_size,
    num_parts,
    feat_mesg_size,
    node_features,
    edge_features,
    node_feat_tids,
    edge_feat_tids,
    edge_data,
    id_lookup,
    ntypes_ntypeid_map,
    ntypes_gnid_range_map,
    etypes_geid_range_map,
    ntid_ntype_map,
    schema_map,
):
    """
    Wrapper function which is used to shuffle graph data on all the processes.

    Parameters:
    -----------
    rank : int
        rank of the current process
    world_size : int
        total no. of participating processes.
    num_parts : int
        total no. of graph partitions.
    node_feautres : dicitonary
        dictionry where node_features are stored and this information is read from the appropriate
        node features file which belongs to the current process
    edge_features : dictionary
        dictionary where edge_features are stored. This information is read from the appropriate
        edge feature files whose ownership is assigned to the current process
    node_feat_tids: dictionary
        in which keys are node-type names and values are triplets. Each triplet has node-feature name
        and the starting and ending type ids of the node-feature data read from the corresponding
        node feature data file read by current process. Each node type may have several features and
        hence each key may have several triplets.
    edge_feat_tids : dictionary
        a dictionary in which keys are edge-type names and values are triplets of the format
        <feat-name, start-per-type-idx, end-per-type-idx>. This triplet is used to identify
        the chunk of feature data for which current process is responsible for
    edge_data : dictionary
        dictionary which is used to store edge information as read from appropriate files assigned
        to each process.
    id_lookup : instance of class DistLookupService
       Distributed lookup service used to map global-nids to respective partition-ids and
       shuffle-global-nids
    ntypes_ntypeid_map : dictionary
        mappings between node type names and node type ids
    ntypes_gnid_range_map : dictionary
        mapping between node type names and global_nids which belong to the keys in this dictionary
    etypes_geid_range_map : dictionary
        mapping between edge type names and global_eids which are assigned to the edges of this
        edge_type
    ntid_ntype_map : dictionary
        mapping between node type id and no of nodes which belong to each node_type_id
    schema_map : dictionary
        is the data structure read from the metadata json file for the input graph

    Returns:
    --------
    dictionary :
        the input argument, node_data dictionary, is updated with the node data received from other processes
        in the world. The node data is received by each rank in the process of data shuffling.
    dictionary :
        node features dictionary which has node features for the nodes which are owned by the current
        process
    dictionary :
        list of global_nids for the nodes whose node features are received when node features shuffling was
        performed in the `exchange_features` function call
    dictionary :
        the input argument, edge_data dictionary, is updated with the edge data received from other processes
        in the world. The edge data is received by each rank in the process of data shuffling.
    dictionary :
        edge features dictionary which has edge features. These destination end points of these edges
        are owned by the current process
    dictionary :
        list of global_eids for the edges whose edge features are received when edge features shuffling
        was performed in the `exchange_features` function call
    """
    memory_snapshot("ShuffleNodeFeaturesBegin: ", rank)
    logging.info(f"[Rank: {rank} - node_feat_tids - {node_feat_tids}")
    rcvd_node_features, rcvd_global_nids = exchange_features(
        rank,
        world_size,
        num_parts,
        node_feat_tids,
        ntypes_gnid_range_map,
        id_lookup,
        node_features,
        constants.STR_NODE_FEATURES,
        None,
    )
    dist.barrier()
    memory_snapshot("ShuffleNodeFeaturesComplete: ", rank)
    logging.info(f"[Rank: {rank}] Done with node features exchange.")

    rcvd_edge_features, rcvd_global_eids = exchange_features(
        rank,
        world_size,
        num_parts,
        feat_mesg_size,
        edge_feat_tids,
        etypes_geid_range_map,
        id_lookup,
        edge_features,
        constants.STR_EDGE_FEATURES,
        edge_data,
    )
    dist.barrier()
    logging.info(f"[Rank: {rank}] Done with edge features exchange.")

    node_data = gen_node_data(
        rank, world_size, num_parts, id_lookup, ntid_ntype_map, schema_map
    )
    dist.barrier()
    memory_snapshot("NodeDataGenerationComplete: ", rank)

    edge_data = exchange_edge_data(
        rank, world_size, num_parts, edge_data, id_lookup
    )
    dist.barrier()
    memory_snapshot("ShuffleEdgeDataComplete: ", rank)
    return (
        node_data,
        rcvd_node_features,
        rcvd_global_nids,
        edge_data,
        rcvd_edge_features,
        rcvd_global_eids,
    )


def read_dataset(rank, world_size, id_lookup, params, schema_map, ntype_counts):
    """
    This function gets the dataset and performs post-processing on the data which is read from files.
    Additional information(columns) are added to nodes metadata like owner_process, global_nid which
    are later used in processing this information. For edge data, which is now a dictionary, we add new columns
    like global_edge_id and owner_process. Augmenting these data structure helps in processing these data structures
    when data shuffling is performed.

    Parameters:
    -----------
    rank : int
        rank of the current process
    world_size : int
        total no. of processes instantiated
    id_lookup : instance of class DistLookupService
       Distributed lookup service used to map global-nids to respective partition-ids and
       shuffle-global-nids
    params : argparser object
        argument parser object to access command line arguments
    schema_map : dictionary
        dictionary created by reading the input graph metadata json file

    Returns :
    ---------
    dictionary
        in which keys are node-type names and values are are tuples representing the range of ids
        for nodes to be read by the current process
    dictionary
        node features which is a dictionary where keys are feature names and values are feature
        data as multi-dimensional tensors
    dictionary
        in which keys are node-type names and values are triplets. Each triplet has node-feature name
        and the starting and ending type ids of the node-feature data read from the corresponding
        node feature data file read by current process. Each node type may have several features and
        hence each key may have several triplets.
    dictionary
        edge data information is read from edges.txt and additional columns are added such as
        owner process for each edge.
    dictionary
        edge features which is also a dictionary, similar to node features dictionary
    dictionary
        a dictionary in which keys are edge-type names and values are tuples indicating the range of ids
        for edges read by the current process.
    dictionary
        a dictionary in which keys are edge-type names and values are triplets,
        (edge-feature-name, start_type_id, end_type_id). These type_ids are indices in the edge-features
        read by the current process. Note that each edge-type may have several edge-features.
    """
    edge_features = {}
    (
        node_features,
        node_feat_tids,
        edge_data,
        edge_typecounts,
        edge_tids,
        edge_features,
        edge_feat_tids,
    ) = get_dataset(
        params.input_dir,
        params.graph_name,
        rank,
        world_size,
        params.num_parts,
        schema_map,
        ntype_counts,
    )
    # Synchronize so that everybody completes reading dataset from disk
    dist.barrier()
    logging.info(f"[Rank: {rank}] Done reading dataset {params.input_dir}")
    dist.barrier()  # SYNCH

    edge_data = augment_edge_data(
        edge_data, id_lookup, edge_tids, rank, world_size, params.num_parts
    )
    dist.barrier()  # SYNCH
    logging.info(
        f"[Rank: {rank}] Done augmenting edge_data: {len(edge_data)}, {edge_data[constants.GLOBAL_SRC_ID].shape}"
    )

    return (
        node_features,
        node_feat_tids,
        edge_data,
        edge_typecounts,
        edge_features,
        edge_feat_tids,
    )


def reorder_data(num_parts, world_size, data, key):
    """
    Auxiliary function used to sort node and edge data for the input graph.

    Parameters:
    -----------
    num_parts : int
        total no. of partitions
    world_size : int
        total number of nodes used in this execution
    data : dictionary
        which is used to store the node and edge data for the input graph
    key : string
        specifies the column which is used to determine the sort order for
        the remaining columns

    Returns:
    --------
    dictionary
        same as the input dictionary, but with reordered columns (values in
        the dictionary), as per the np.argsort results on the column specified
        by the ``key`` column
    """
    for local_part_id in range(num_parts // world_size):
        sorted_idx = data[key + "/" + str(local_part_id)].argsort()
        for k, v in data.items():
            tokens = k.split("/")
            assert len(tokens) == 2
            if tokens[1] == str(local_part_id):
                data[k] = v[sorted_idx]
        sorted_idx = None
    gc.collect()
    return data


def gen_dist_partitions(rank, world_size, params):
    """
    Function which will be executed by all Gloo processes to begin execution of the pipeline.
    This function expects the input dataset is split across multiple file format.

    Input dataset and its file structure is described in metadata json file which is also part of the
    input dataset. On a high-level, this metadata json file contains information about the following items
    a) Nodes metadata, It is assumed that nodes which belong to each node-type are split into p files
       (wherer `p` is no. of partitions).
    b) Similarly edge metadata contains information about edges which are split into p-files.
    c) Node and Edge features, it is also assumed that each node (and edge) feature, if present, is also
       split into `p` files.

    For example, a sample metadata json file might be as follows: :
    (In this toy example, we assume that we have "m" node-types, "k" edge types, and for node_type = ntype0-name
     we have two features namely feat0-name and feat1-name. Please note that the node-features are also split into
     `p` files. This will help in load-balancing during data-shuffling phase).

    Terminology used to identify any particular "id" assigned to nodes, edges or node features. Prefix "global" is
    used to indicate that this information is either read from the input dataset or autogenerated based on the information
    read from input dataset files. Prefix "type" is used to indicate a unique id assigned to either nodes or edges.
    For instance, type_node_id means that a unique id, with a given node type,  assigned to a node. And prefix "shuffle"
    will be used to indicate a unique id, across entire graph, assigned to either a node or an edge. For instance,
    SHUFFLE_GLOBAL_NID means a unique id which is assigned to a node after the data shuffle is completed.

    Some high-level notes on the structure of the metadata json file.
    1. path(s) mentioned in the entries for nodes, edges and node-features files can be either absolute or relative.
       if these paths are relative, then it is assumed that they are relative to the folder from which the execution is
       launched.
    2. The id_startx and id_endx represent the type_node_id and type_edge_id respectively for nodes and edge data. This
       means that these ids should match the no. of nodes/edges read from any given file. Since these are type_ids for
       the nodes and edges in any given file, their global_ids can be easily computed as well.

    {
        "graph_name" : xyz,
        "node_type" : ["ntype0-name", "ntype1-name", ....], #m node types
        "num_nodes_per_chunk" : [
            [a0, a1, ...a<p-1>], #p partitions
            [b0, b1, ... b<p-1>],
            ....
            [c0, c1, ..., c<p-1>] #no, of node types
        ],
        "edge_type" : ["src_ntype:edge_type:dst_ntype", ....], #k edge types
        "num_edges_per_chunk" : [
            [a0, a1, ...a<p-1>], #p partitions
            [b0, b1, ... b<p-1>],
            ....
            [c0, c1, ..., c<p-1>] #no, of edge types
        ],
        "node_data" : {
            "ntype0-name" : {
                "feat0-name" : {
                    "format" : {"name": "numpy"},
                    "data" :   [ #list of lists
                        ["<path>/feat-0.npy", 0, id_end0],
                        ["<path>/feat-1.npy", id_start1, id_end1],
                        ....
                        ["<path>/feat-<p-1>.npy", id_start<p-1>, id_end<p-1>]
                    ]
                },
                "feat1-name" : {
                    "format" : {"name": "numpy"},
                    "data" : [ #list of lists
                        ["<path>/feat-0.npy", 0, id_end0],
                        ["<path>/feat-1.npy", id_start1, id_end1],
                        ....
                        ["<path>/feat-<p-1>.npy", id_start<p-1>, id_end<p-1>]
                    ]
                }
            }
        },
        "edges": { #k edge types
            "src_ntype:etype0-name:dst_ntype" : {
                "format": {"name" : "csv", "delimiter" : " "},
                "data" : [
                    ["<path>/etype0-name-0.txt", 0, id_end0], #These are type_edge_ids for edges of this type
                    ["<path>/etype0-name-1.txt", id_start1, id_end1],
                    ...,
                    ["<path>/etype0-name-<p-1>.txt", id_start<p-1>, id_end<p-1>]
                ]
            },
            ...,
            "src_ntype:etype<k-1>-name:dst_ntype" : {
                "format": {"name" : "csv", "delimiter" : " "},
                "data" : [
                    ["<path>/etype<k-1>-name-0.txt", 0, id_end0],
                    ["<path>/etype<k-1>-name-1.txt", id_start1, id_end1],
                    ...,
                    ["<path>/etype<k-1>-name-<p-1>.txt", id_start<p-1>, id_end<p-1>]
                ]
            },
        },
    }

    The function performs the following steps:
    1. Reads the metis partitions to identify the owner process of all the nodes in the entire graph.
    2. Reads the input data set, each partitipating process will map to a single file for the edges,
        node-features and edge-features for each node-type and edge-types respectively. Using nodes metadata
        information, nodes which are owned by a given process are generated to optimize communication to some
        extent.
    3. Now each process shuffles the data by identifying the respective owner processes using metis
        partitions.
        a. To identify owner processes for nodes, metis partitions will be used.
        b. For edges, the owner process of the destination node will be the owner of the edge as well.
        c. For node and edge features, identifying the owner process is a little bit involved.
            For this purpose, graph metadata json file is used to first map the locally read node features
            to their global_nids. Now owner process is identified using metis partitions for these global_nids
            to retrieve shuffle_global_nids. A similar process is used for edge_features as well.
        d. After all the data shuffling is done, the order of node-features may be different when compared to
            their global_type_nids. Node- and edge-data are ordered by node-type and edge-type respectively.
            And now node features and edge features are re-ordered to match the order of their node- and edge-types.
    4. Last step is to create the DGL objects with the data present on each of the processes.
        a. DGL objects for nodes, edges, node- and edge- features.
        b. Metadata is gathered from each process to create the global metadata json file, by process rank = 0.

    Parameters:
    ----------
    rank : int
        integer representing the rank of the current process in a typical distributed implementation
    world_size : int
        integer representing the total no. of participating processes in a typical distributed implementation
    params : argparser object
        this object, key value pairs, provides access to the command line arguments from the runtime environment
    """
    global_start = timer()
    logging.info(
        f"[Rank: {rank}] Starting distributed data processing pipeline..."
    )
    memory_snapshot("Pipeline Begin: ", rank)

    # init processing
    schema_map = read_json(os.path.join(params.input_dir, params.schema))

    # The resources, which are node-id to partition-id mappings, are split
    # into `world_size` number of parts, where each part can be mapped to
    # each physical node.
    id_lookup = DistLookupService(
        os.path.join(params.input_dir, params.partitions_dir),
        schema_map[constants.STR_NODE_TYPE],
        rank,
        world_size,
        params.num_parts,
    )

    # get the id to name mappings here.
    ntypes_ntypeid_map, ntypes, ntypeid_ntypes_map = get_node_types(schema_map)
    etypes_etypeid_map, etypes, etypeid_etypes_map = get_edge_types(schema_map)
    logging.info(
        f"[Rank: {rank}] Initialized metis partitions and node_types map..."
    )

    # Initialize distributed lookup service for partition-id and shuffle-global-nids mappings
    # for global-nids
    _, global_nid_ranges = get_idranges(
        schema_map[constants.STR_NODE_TYPE],
        get_ntype_counts_map(
            schema_map[constants.STR_NODE_TYPE],
            schema_map[constants.STR_NUM_NODES_PER_TYPE],
        ),
    )
    id_map = dgl.distributed.id_map.IdMap(global_nid_ranges)
    id_lookup.set_idMap(id_map)

    # read input graph files and augment these datastructures with
    # appropriate information (global_nid and owner process) for node and edge data
    (
        node_features,
        node_feat_tids,
        edge_data,
        edge_typecounts,
        edge_features,
        edge_feat_tids,
    ) = read_dataset(
        rank,
        world_size,
        id_lookup,
        params,
        schema_map,
        get_ntype_counts_map(
            schema_map[constants.STR_NODE_TYPE],
            schema_map[constants.STR_NUM_NODES_PER_TYPE],
        ),
    )
    logging.info(
        f"[Rank: {rank}] Done augmenting file input data with auxilary columns"
    )
    memory_snapshot("DatasetReadComplete: ", rank)

    # send out node and edge data --- and appropriate features.
    # this function will also stitch the data recvd from other processes
    # and return the aggregated data
    # ntypes_gnid_range_map = get_gnid_range_map(node_tids)
    # etypes_geid_range_map = get_gnid_range_map(edge_tids)
    ntypes_gnid_range_map = get_gid_offsets(
        schema_map[constants.STR_NODE_TYPE],
        get_ntype_counts_map(
            schema_map[constants.STR_NODE_TYPE],
            schema_map[constants.STR_NUM_NODES_PER_TYPE],
        ),
    )
    etypes_geid_range_map = get_gid_offsets(
        schema_map[constants.STR_EDGE_TYPE], edge_typecounts
    )

    (
        node_data,
        rcvd_node_features,
        rcvd_global_nids,
        edge_data,
        rcvd_edge_features,
        rcvd_global_eids,
    ) = exchange_graph_data(
        rank,
        world_size,
        params.num_parts,
        params.feature_mesg_size,
        node_features,
        edge_features,
        node_feat_tids,
        edge_feat_tids,
        edge_data,
        id_lookup,
        ntypes_ntypeid_map,
        ntypes_gnid_range_map,
        etypes_geid_range_map,
        ntypeid_ntypes_map,
        schema_map,
    )
    gc.collect()
    logging.info(f"[Rank: {rank}] Done with data shuffling...")
    memory_snapshot("DataShuffleComplete: ", rank)

    # sort node_data by ntype
    node_data = reorder_data(
        params.num_parts, world_size, node_data, constants.NTYPE_ID
    )
    logging.info(f"[Rank: {rank}] Sorted node_data by node_type")
    memory_snapshot("NodeDataSortComplete: ", rank)

    # resolve global_ids for nodes
    # Synchronize before assigning shuffle-global-ids to nodes
    dist.barrier()
    assign_shuffle_global_nids_nodes(
        rank, world_size, params.num_parts, node_data
    )
    logging.info(f"[Rank: {rank}] Done assigning global-ids to nodes...")
    memory_snapshot("ShuffleGlobalID_Nodes_Complete: ", rank)

    # shuffle node feature according to the node order on each rank.
    for ntype_name in ntypes:
        featnames = get_ntype_featnames(ntype_name, schema_map)
        for featname in featnames:
            # if a feature name exists for a node-type, then it should also have
            # feature data as well. Hence using the assert statement.
            for local_part_id in range(params.num_parts // world_size):
                feature_key = (
                    ntype_name + "/" + featname + "/" + str(local_part_id)
                )
                assert feature_key in rcvd_global_nids
                global_nids = rcvd_global_nids[feature_key]

                _, idx1, _ = np.intersect1d(
                    node_data[constants.GLOBAL_NID + "/" + str(local_part_id)],
                    global_nids,
                    return_indices=True,
                )
                shuffle_global_ids = node_data[
                    constants.SHUFFLE_GLOBAL_NID + "/" + str(local_part_id)
                ][idx1]
                feature_idx = shuffle_global_ids.argsort()

                rcvd_node_features[feature_key] = rcvd_node_features[
                    feature_key
                ][feature_idx]
    memory_snapshot("ReorderNodeFeaturesComplete: ", rank)

    # Sort edge_data by etype
    edge_data = reorder_data(
        params.num_parts, world_size, edge_data, constants.ETYPE_ID
    )
    logging.info(f"[Rank: {rank}] Sorted edge_data by edge_type")
    memory_snapshot("EdgeDataSortComplete: ", rank)

    # Synchronize before assigning shuffle-global-nids for edges end points.
    dist.barrier()
    shuffle_global_eid_offsets = assign_shuffle_global_nids_edges(
        rank, world_size, params.num_parts, edge_data
    )
    logging.info(f"[Rank: {rank}] Done assigning global_ids to edges ...")

    memory_snapshot("ShuffleGlobalID_Edges_Complete: ", rank)

    # Shuffle edge features according to the edge order on each rank.
    for etype_name in etypes:
        featnames = get_etype_featnames(etype_name, schema_map)
        for featname in featnames:
            for local_part_id in range(params.num_parts // world_size):
                feature_key = (
                    etype_name + "/" + featname + "/" + str(local_part_id)
                )
                assert feature_key in rcvd_global_eids
                global_eids = rcvd_global_eids[feature_key]

                _, idx1, _ = np.intersect1d(
                    edge_data[constants.GLOBAL_EID + "/" + str(local_part_id)],
                    global_eids,
                    return_indices=True,
                )
                shuffle_global_ids = edge_data[
                    constants.SHUFFLE_GLOBAL_EID + "/" + str(local_part_id)
                ][idx1]
                feature_idx = shuffle_global_ids.argsort()

                rcvd_edge_features[feature_key] = rcvd_edge_features[
                    feature_key
                ][feature_idx]

    # determine global-ids for edge end-points
    # Synchronize before retrieving shuffle-global-nids for edges end points.
    dist.barrier()
    edge_data = lookup_shuffle_global_nids_edges(
        rank, world_size, params.num_parts, edge_data, id_lookup, node_data
    )
    logging.info(
        f"[Rank: {rank}] Done resolving orig_node_id for local node_ids..."
    )
    memory_snapshot("ShuffleGlobalID_Lookup_Complete: ", rank)

    def prepare_local_data(src_data, local_part_id):
        local_data = {}
        for k, v in src_data.items():
            tokens = k.split("/")
            if tokens[len(tokens) - 1] == str(local_part_id):
                local_data["/".join(tokens[:-1])] = v
        return local_data

    # create dgl objects here
    output_meta_json = {}
    start = timer()

    graph_formats = None
    if params.graph_formats:
        graph_formats = params.graph_formats.split(",")

    for local_part_id in range(params.num_parts // world_size):
        # Synchronize for each local partition of the graph object.
        dist.barrier()

        num_edges = shuffle_global_eid_offsets[local_part_id]
        node_count = len(
            node_data[constants.NTYPE_ID + "/" + str(local_part_id)]
        )
        edge_count = len(
            edge_data[constants.ETYPE_ID + "/" + str(local_part_id)]
        )
        local_node_data = prepare_local_data(node_data, local_part_id)
        local_edge_data = prepare_local_data(edge_data, local_part_id)
        (
            graph_obj,
            ntypes_map_val,
            etypes_map_val,
            ntypes_map,
            etypes_map,
            orig_nids,
            orig_eids,
        ) = create_dgl_object(
            schema_map,
            rank + local_part_id * world_size,
            local_node_data,
            local_edge_data,
            num_edges,
            get_ntype_counts_map(
                schema_map[constants.STR_NODE_TYPE],
                schema_map[constants.STR_NUM_NODES_PER_TYPE],
            ),
            edge_typecounts,
            params.save_orig_nids,
            params.save_orig_eids,
        )
        sort_etypes = len(etypes_map) > 1
        local_node_features = prepare_local_data(
            rcvd_node_features, local_part_id
        )
        local_edge_features = prepare_local_data(
            rcvd_edge_features, local_part_id
        )
        write_dgl_objects(
            graph_obj,
            local_node_features,
            local_edge_features,
            params.output,
            rank + (local_part_id * world_size),
            orig_nids,
            orig_eids,
            graph_formats,
            sort_etypes,
        )
        memory_snapshot("DiskWriteDGLObjectsComplete: ", rank)

        # get the meta-data
        json_metadata = create_metadata_json(
            params.graph_name,
            node_count,
            edge_count,
            local_part_id * world_size + rank,
            params.num_parts,
            ntypes_map_val,
            etypes_map_val,
            ntypes_map,
            etypes_map,
            params.output,
        )
        output_meta_json[
            "local-part-id-" + str(local_part_id * world_size + rank)
        ] = json_metadata
        memory_snapshot("MetadataCreateComplete: ", rank)

    if rank == 0:
        # get meta-data from all partitions and merge them on rank-0
        metadata_list = gather_metadata_json(output_meta_json, rank, world_size)
        metadata_list[0] = output_meta_json
        write_metadata_json(
            metadata_list,
            params.output,
            params.graph_name,
            world_size,
            params.num_parts,
        )
    else:
        # send meta-data to Rank-0 process
        gather_metadata_json(output_meta_json, rank, world_size)
    end = timer()
    logging.info(
        f"[Rank: {rank}] Time to create dgl objects: {timedelta(seconds = end - start)}"
    )
    memory_snapshot("MetadataWriteComplete: ", rank)

    global_end = timer()
    logging.info(
        f"[Rank: {rank}] Total execution time of the program: {timedelta(seconds = global_end - global_start)}"
    )
    memory_snapshot("PipelineComplete: ", rank)


def single_machine_run(params):
    """Main function for distributed implementation on a single machine

    Parameters:
    -----------
    params : argparser object
        Argument Parser structure with pre-determined arguments as defined
        at the bottom of this file.
    """
    log_params(params)
    processes = []
    mp.set_start_method("spawn")

    # Invoke `target` function from each of the spawned process for distributed
    # implementation
    for rank in range(params.world_size):
        p = mp.Process(
            target=run,
            args=(rank, params.world_size, gen_dist_partitions, params),
        )
        p.start()
        processes.append(p)

    for p in processes:
        p.join()


def run(rank, world_size, func_exec, params, backend="gloo"):
    """
    Init. function which is run by each process in the Gloo ProcessGroup

    Parameters:
    -----------
    rank : integer
        rank of the process
    world_size : integer
        number of processes configured in the Process Group
    proc_exec : function name
        function which will be invoked which has the logic for each process in the group
    params : argparser object
        argument parser object to access the command line arguments
    backend : string
        string specifying the type of backend to use for communication
    """
    os.environ["MASTER_ADDR"] = "127.0.0.1"
    os.environ["MASTER_PORT"] = "29500"

    # create Gloo Process Group
    dist.init_process_group(
        backend,
        rank=rank,
        world_size=world_size,
        timeout=timedelta(seconds=5 * 60),
    )

    # Invoke the main function to kick-off each process
    func_exec(rank, world_size, params)


def multi_machine_run(params):
    """
    Function to be invoked when executing data loading pipeline on multiple machines

    Parameters:
    -----------
    params : argparser object
        argparser object providing access to command line arguments.
    """
    rank = int(os.environ["RANK"])

    # init the gloo process group here.
    dist.init_process_group(
        backend="gloo",
        rank=rank,
        world_size=params.world_size,
        timeout=timedelta(seconds=params.process_group_timeout),
    )
    logging.info(f"[Rank: {rank}] Done with process group initialization...")

    # invoke the main function here.
    gen_dist_partitions(rank, params.world_size, params)
    logging.info(
        f"[Rank: {rank}] Done with Distributed data processing pipeline processing."
    )
