#!/usr/bin/env bash
#
# Builds the bundled basemap end to end.
#
#   ./Tools/build-basemap.sh <work-dir>
#
# Needs osmium-tool (brew install osmium-tool) and about 3 GB of free space in
# the work directory. Downloads are skipped if they are already there, so a
# re-run after a failure costs nothing.
set -euo pipefail

WORK="${1:-build/basemap}"
OUT="App/darkworld/Resources/Basemap.noxbase"
mkdir -p "$WORK"

COAST_URL="https://naciscdn.org/naturalearth/10m/physical/ne_10m_coastline.zip"
PBF_URL="https://download.geofabrik.de/asia/japan-latest.osm.pbf"
UA="noxveil-basemap-build/0.1"

say() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }

say "Natural Earth coastline"
if [[ ! -f "$WORK/ne_10m_coastline/ne_10m_coastline.shp" ]]; then
  curl -L --fail -A "$UA" -o "$WORK/coastline.zip" "$COAST_URL"
  unzip -o -q "$WORK/coastline.zip" -d "$WORK/ne_10m_coastline"
fi
echo "  have $WORK/ne_10m_coastline/ne_10m_coastline.shp"

say "OpenStreetMap extract for Japan"
if [[ ! -f "$WORK/japan-latest.osm.pbf" ]]; then
  echo "  downloading about 2.4 GB"
  curl -L --fail -A "$UA" -o "$WORK/japan-latest.osm.pbf" "$PBF_URL"
fi
echo "  have $WORK/japan-latest.osm.pbf ($(du -h "$WORK/japan-latest.osm.pbf" | cut -f1))"

say "Filtering to the roads a person can walk on"
# Everything down to the street outside the house. Service roads -- car park
# aisles, driveways, delivery lanes -- are left out: they are the largest class
# in the extract and almost none of it is anywhere a walk goes.
if [[ ! -f "$WORK/roads.osm.pbf" ]]; then
  osmium tags-filter --overwrite \
    "$WORK/japan-latest.osm.pbf" \
    w/highway=motorway,motorway_link,trunk,trunk_link,primary,primary_link,secondary,secondary_link,tertiary,tertiary_link,residential,unclassified,living_street,pedestrian,footway,path,steps,cycleway,track \
    -o "$WORK/roads.osm.pbf"
fi
echo "  $(du -h "$WORK/roads.osm.pbf" | cut -f1) of roads"

say "Exporting line geometry"
if [[ ! -f "$WORK/roads.geojsonseq" ]]; then
  osmium export --overwrite \
    "$WORK/roads.osm.pbf" \
    -f geojsonseq --geometry-types=linestring \
    -o "$WORK/roads.geojsonseq"
fi
echo "  $(du -h "$WORK/roads.geojsonseq" | cut -f1) of GeoJSON"

# The built map is kept in the work directory as well as installed, and the
# work copy is what gets reinstalled when the app's copy goes missing.
#
# It goes missing more easily than it looks. The file is not in the repository,
# so checking out any commit from before it left -- or cleaning ignored files
# -- takes it away, and the app then launches saying it has no map to draw.
# Rebuilding costs six minutes; copying costs a second.
say "Building $OUT"
if [[ ! -f "$OUT" && -f "$WORK/Basemap.noxbase" ]]; then
  echo "  $OUT is missing; restoring the copy in $WORK"
  mkdir -p "$(dirname "$OUT")"
  cp "$WORK/Basemap.noxbase" "$OUT"
  echo "  restored $(du -h "$OUT" | cut -f1)"
  exit 0
fi

python3 Tools/build-basemap.py \
  --coastline "$WORK/ne_10m_coastline/ne_10m_coastline.shp" \
  --roads-geojsonseq "$WORK/roads.geojsonseq" \
  --japan-only \
  --output "$WORK/Basemap.noxbase"

mkdir -p "$(dirname "$OUT")"
cp "$WORK/Basemap.noxbase" "$OUT"
echo "  installed at $OUT"
