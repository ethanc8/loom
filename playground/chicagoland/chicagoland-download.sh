#!/bin/bash

mkdir -p temp
curl -L https://www.transitchicago.com/downloads/sch_data/google_transit.zip -o temp/gtfs/cta.zip
unzip temp/gtfs/cta.zip -d temp/gtfs/cta
curl -L https://www.pacebus.com/sites/default/files/2026-05/GTFS.zip -o temp/gtfs/pace.zip
unzip temp/gtfs/pace.zip -d temp/gtfs/pace
curl -L https://schedules.metrarail.com/gtfs/schedule.zip -o temp/gtfs/metra.zip
unzip temp/gtfs/metra.zip -d temp/gtfs/metra

