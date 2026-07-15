#!/bin/bash

export PATH="$(realpath ../../build):$PATH"

date
echo "about to run freq"
python3 ../../scripts/freq.py --config freq.toml --gtfs temp/gtfs/chicagoland temp/chicagoland-bus-topo > temp/chicagoland-bus-freq 2>temp/freq-log.txt

date
echo "about to run loom"
loom < temp/chicagoland-bus-freq > temp/chicagoland-bus-loom

date
echo "about to run transitmap"
transitmap -l --render-dir-markers < temp/chicagoland-bus-loom > temp/chicagoland-bus-loom.svg

date
