#!/bin/bash

export PATH="$(realpath ../../build):$PATH"

python3 ../../scripts/merge_gtfs.py temp/gtfs/cta temp/gtfs/metra temp/gtfs/pace temp/gtfs/chicagoland

date
echo "about to run gtfs2graph"
gtfs2graph -m bus temp/gtfs/chicagoland > temp/chicagoland-bus-graph

date
echo "about to run topo"
topo < temp/chicagoland-bus-graph > temp/chicagoland-bus-topo

date
