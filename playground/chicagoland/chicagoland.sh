#!/bin/bash

python3 merge_gtfs.py gtfs/cta gtfs/metra gtfs/pace gtfs/chicagoland
gtfs2graph -m metro,rail gtfs/chicagoland > chicagoland-rail-graph
topo < chicagoland-rail-graph > chicagoland-rail-topo
loom < chicagoland-rail-topo > chicagoland-rail-loom
transitmap -l --render-dir-markers < chicagoland-rail-loom > chicagoland-rail-loom.svg
octi < chicagoland-rail-loom > chicagoland-rail-octi
transitmap -l --render-dir-markers < chicagoland-rail-octi > chicagoland-rail-octi.svg

gtfs2graph gtfs/chicagoland > chicagoland-all-graph
topo < chicagoland-all-graph > chicagoland-all-topo
loom < chicagoland-all-topo > chicagoland-all-loom
transitmap -l --render-dir-markers < chicagoland-all-loom > chicagoland-all-loom.svg
