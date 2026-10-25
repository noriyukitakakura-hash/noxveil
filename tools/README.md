# The map Noxveil carries

`build-basemap.sh` downloads the public sources and `build-basemap.py` turns
them into `Basemap.noxbase`, the single file the app ships and reads.

```sh
./build-basemap.sh build/basemap
```

Needs `osmium-tool` (`brew install osmium-tool`) and about 7 GB of working
space. Downloads and intermediate files are skipped when already present.

**This is here to satisfy the Open Database License.** The file the app ships
is a derived database made from OpenStreetMap, so it is offered under ODbL —
and rather than hand out a 230 MB blob, these are the scripts that produce it
from the public sources, byte for byte. Anybody can make the same file instead
of taking our word for what is in it.

| source | licence |
| --- | --- |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) roads, via [Geofabrik](https://download.geofabrik.de/) | ODbL — attribution required |
| [Natural Earth](https://www.naturalearthdata.com/) 10 m coastline | public domain |

The roads kept are every one a person can walk on — down to footways, paths
and steps — because that is where a life is actually lived. Service roads are
left out: the largest class in the extract, and almost none of it anywhere a
walk goes.
