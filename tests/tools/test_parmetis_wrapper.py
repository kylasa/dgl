import argparse
import dgl
import json
import numpy as np
import os
import sys
from pathlib import Path
import tempfile
import torch
import logging
import platform

from dgl.data.utils import load_tensors, load_graphs
from chunk_graph import chunk_graph

def test_parmetis_wrapper():
    # Step0: prepare chunked graph data format.

    # Create a synthetic mini graph (similar to MAG240 dataset).
    num_institutions = 20
    num_authors = 100
    num_papers = 600
    num_cite_edges = 2000
    num_write_edges = 1000
    num_affiliate_edges = 200
    
    num_nodes = num_authors + num_papers + num_institutions
    num_edges = num_cite_edges + num_write_edges + num_affiliate_edges
    num_constraints = 3 + 3

    def rand_edges(num_src, num_dst, num_edges):
        eids = np.random.choice(num_src * num_dst, num_edges, replace=False)
        src = torch.from_numpy(eids // num_dst)
        dst = torch.from_numpy(eids % num_dst)
        return src, dst

    # Create the no. of edges and build a dictioinary to store them. 
    data_dict = {
        ('paper', 'cites', 'paper'): rand_edges(num_papers, num_papers, num_cite_edges),
        ('author', 'writes', 'paper'): rand_edges(num_authors, num_papers, num_write_edges),
        ('author', 'affiliated_with', 'institution'): rand_edges(num_authors, num_institutions, num_affiliate_edges)
    }
    src, dst = data_dict[('author', 'writes', 'paper')]
    data_dict[('paper', 'rev_writes', 'author')] = (dst, src)
    g = dgl.heterograph(data_dict)

    # Create features for the node type paper.
    num_paper_feats = 3
    paper_feat = np.random.randn(num_papers, num_paper_feats)
    num_classes = 4
    paper_label = np.random.choice(num_classes, num_papers)
    paper_year = np.random.choice(2022, num_papers)

    paper_train_mask = np.random.randint(0, 2, num_papers)
    paper_test_mask = np.random.randint(0, 2, num_papers)
    paper_val_mask = np.random.randint(0, 2, num_papers)
    
    author_train_mask = np.random.randint(0, 2, num_authors)
    author_test_mask = np.random.randint(0, 2, num_authors)
    author_val_mask = np.random.randint(0, 2, num_authors)

    inst_train_mask = np.random.randint(0, 2, num_institutions)
    inst_test_mask = np.random.randint(0, 2, num_institutions)
    inst_val_mask = np.random.randint(0, 2, num_institutions)

    # edge features
    cite_count = np.random.choice(10, num_cite_edges)
    write_year = np.random.choice(2022, num_write_edges)

    # Save node features in appropriate files.
    with tempfile.TemporaryDirectory() as root_dir:
        print('root_dir:', root_dir)
        input_dir = os.path.join(root_dir, 'data_test')
        os.makedirs(input_dir)
        for sub_d in ['paper', 'cites', 'writes', 'author', 'institution']:
            os.makedirs(os.path.join(input_dir, sub_d))

        paper_feat_path = os.path.join(input_dir, 'paper/feat.npy')
        with open(paper_feat_path, 'wb') as f:
            np.save(f, paper_feat)

        paper_label_path = os.path.join(input_dir, 'paper/label.npy')
        with open(paper_label_path, 'wb') as f:
            np.save(f, paper_label)

        paper_year_path = os.path.join(input_dir, 'paper/year.npy')
        with open(paper_year_path, 'wb') as f:
            np.save(f, paper_year)

        paper_train_mask_path = os.path.join(input_dir, 'paper/train_mask.npy')
        with open(paper_train_mask_path, 'wb') as f:
            np.save(f, paper_train_mask)

        paper_test_mask_path = os.path.join(input_dir, 'paper/test_mask.npy')
        with open(paper_test_mask_path, 'wb') as f:
            np.save(f, paper_test_mask)

        paper_val_mask_path = os.path.join(input_dir, 'paper/val_mask.npy')
        with open(paper_val_mask_path, 'wb') as f:
            np.save(f, paper_val_mask)

        author_train_mask_path = os.path.join(input_dir, 'author/train_mask.npy')
        with open(author_train_mask_path, 'wb') as f:
            np.save(f, author_train_mask)

        author_test_mask_path = os.path.join(input_dir, 'author/test_mask.npy')
        with open(author_test_mask_path, 'wb') as f:
            np.save(f, author_test_mask)

        author_val_mask_path = os.path.join(input_dir, 'author/val_mask.npy')
        with open(author_val_mask_path, 'wb') as f:
            np.save(f, author_val_mask)

        inst_train_mask_path = os.path.join(input_dir, 'institution/train_mask.npy')
        with open(inst_train_mask_path, 'wb') as f:
            np.save(f, inst_train_mask)

        inst_test_mask_path = os.path.join(input_dir, 'institution/test_mask.npy')
        with open(inst_test_mask_path, 'wb') as f:
            np.save(f, inst_test_mask)

        inst_val_mask_path = os.path.join(input_dir, 'institution/val_mask.npy')
        with open(inst_val_mask_path, 'wb') as f:
            np.save(f, inst_val_mask)

        cite_count_path = os.path.join(input_dir, 'cites/count.npy')
        with open(cite_count_path, 'wb') as f:
            np.save(f, cite_count)

        write_year_path = os.path.join(input_dir, 'writes/year.npy')
        with open(write_year_path, 'wb') as f:
            np.save(f, write_year)

        output_dir = os.path.join(root_dir, 'chunked-data')
        num_chunks = 2
        chunk_graph(
            g,
            'mag240m',
            {
                'paper':
                {
                    'feat': paper_feat_path,
                    'train_mask': paper_train_mask_path,
                    'test_mask': paper_test_mask_path,
                    'val_mask': paper_val_mask_path,
                    'label': paper_label_path,
                    'year': paper_year_path
                }, 
                'author':
                {
                    'train_mask': author_train_mask_path,
                    'test_mask': author_test_mask_path, 
                    'val_mask': author_val_mask_path

                },
                'institution':
                {
                    'train_mask': inst_train_mask_path,
                    'test_mask': inst_test_mask_path, 
                    'val_mask': inst_val_mask_path
                }
            },
            {
                'cites':
                {
                    'count': cite_count_path
                },
                'writes': 
                {
                    'year': write_year_path
                },
                # Here same data file is used. 
                # Features can be shared if the dimensions agree
                'rev_writes': 
                {
                    'year': write_year_path
                }
            },
            num_chunks=num_chunks,
            output_path=output_dir)

        # Check metadata.json.
        json_file = os.path.join(output_dir, 'metadata.json')
        assert os.path.isfile(json_file)
        with open(json_file, 'rb') as f:
            meta_data = json.load(f)
        assert meta_data['graph_name'] == 'mag240m'
        graph_name = meta_data['graph_name']
        assert len(meta_data['num_nodes_per_chunk'][0]) == num_chunks

        # Check edge_index.
        output_edge_index_dir = os.path.join(output_dir, 'edge_index')
        for utype, etype, vtype in data_dict.keys():
            filename = ':'.join([utype, etype, vtype])
            for i in range(num_chunks):
                chunk_filename = os.path.join(output_edge_index_dir, filename + str(i) + '.txt')
                assert os.path.isfile(chunk_filename)
                with open(chunk_filename, 'r') as f:
                    header = f.readline()
                    num1, num2 = header.rstrip().split(' ')
                    assert isinstance(int(num1), int)
                    assert isinstance(int(num2), int)

        # Check node_data.
        output_node_data_dir = os.path.join(output_dir, 'node_data', 'paper')
        for feat in ['feat', 'label', 'year']:
            for i in range(num_chunks):
                chunk_filename = f'{feat}-{i}.npy'.format(feat, i)
                chunk_filename = os.path.join(output_node_data_dir, chunk_filename)
                assert os.path.isfile(chunk_filename)
                feat_array = np.load(chunk_filename)
                assert feat_array.shape[0] == num_papers // num_chunks

        # Check edge_data.
        num_edges = {
            'paper:cites:paper': num_cite_edges,
            'author:writes:paper': num_write_edges,
            'paper:rev_writes:author': num_write_edges
        }
        output_edge_data_dir = os.path.join(output_dir, 'edge_data')
        for etype, feat in [
            ['paper:cites:paper', 'count'],
            ['author:writes:paper', 'year'],
            ['paper:rev_writes:author', 'year']
        ]:
            output_edge_sub_dir = os.path.join(output_edge_data_dir, etype)
            for i in range(num_chunks):
                chunk_filename = f'{feat}-{i}.npy'
                chunk_filename = os.path.join(output_edge_sub_dir, chunk_filename)
                assert os.path.isfile(chunk_filename)
                feat_array = np.load(chunk_filename)
                assert feat_array.shape[0] == num_edges[etype] // num_chunks

        # Trigger ParMETIS.
        schema_file = os.path.join(root_dir, 'chunked-data/metadata.json')
        preproc_output_dir = os.path.join(root_dir, 'chunked-data/preproc_output_dir')
        parmetis_output_file = os.path.join(os.getcwd(), f'{graph_name}_part.{num_chunks}')
        partitions_dir = os.path.join(root_dir, 'chunked-data/partitions_dir')
        hostfile = os.path.join(root_dir, 'ip_config.txt')
        with open(hostfile, 'w') as f:
            f.write('127.0.0.1\n')
            f.write('127.0.0.1\n')

        stats_file = os.path.join(root_dir, f'chunked-data/{graph_name}_stats.txt')
        with open(stats_file, 'w') as f:
            f.write(f'{num_nodes} {num_edges} {num_constraints}')

        prev_working_directory = os.getcwd()
        os.chdir(os.path.join(root_dir, 'chunked-data'))
        env = dict(os.environ)
        dgl_home = env["DGL_HOME"]
        if dgl_home[-1] != "/": 
            dgl_home += "/"
        parmetis_cmd = f'python3 {dgl_home}tools/distpartitioning/parmetis_wrapper.py '\
                       f'--schema_file {schema_file} '\
                       f'--preproc_output_dir {preproc_output_dir} '\
                       f'--hostfile {hostfile} '\
                       f'--parmetis_output_file {parmetis_output_file} '\
                       f'--partitions_dir {partitions_dir} '
        logging.info(f'Executing the following cmd: {parmetis_cmd}')
        print(parmetis_cmd)
        os.system(parmetis_cmd)

        ntype_count = {
                'author':num_authors,
                'paper':num_papers,
                'institution':num_institutions
                }
        for ntype_name in ['author', 'paper', 'institution']:
            fname = os.path.join(partitions_dir, f'{ntype_name}.txt')
            print(fname)
            assert os.path.isfile(fname)

            # Load and check the partition ids in this file.
            part_ids = np.loadtxt(fname)
            assert part_ids.shape[0] == ntype_count[ntype_name]
            assert np.min(part_ids) == 0
            assert np.max(part_ids) == (num_chunks - 1)

if __name__ == '__main__':
    #Configure logging.
    logging.basicConfig(level='INFO', format=f"[{platform.node()} \
        %(levelname)s %(asctime)s PID:%(process)d] %(message)s")
    test_parmetis_wrapper()
