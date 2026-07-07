# Shapr3D Export Tool

A small desktop utility (PySide6 + sqlite3) to browse local Shapr3D projects and export data.

![Shapr3D Export Tool](images/shapr-export.jpg)

## Disclaimer

This project is intended for educational and research use, and is provided "as is," without any warranty.

By using this project, you acknowledge that you are doing so at your own risk and that you are responsible for ensuring that your use of this project complies with all applicable laws, regulations, and terms of service.

Parasolid is a trademark of Siemens Digital Industries Software, and Shapr3D is a trademark of Shapr3D Zrt. This project is independent and is not affiliated with, endorsed by, or sponsored by Siemens or Shapr3D. These names are used only to identify the relevant file formats and software for interoperability purposes.

## Features

- Project browser grid with thumbnail + project name
- Export project to `.shapr`
- Export tessellation to `.stl` (experimental)
- Export Parasolid to `.x_b`

## How It Works

The app reads your local Shapr3D project database and loads projects. You can select a project and choose to export it in different formats.

Tessellation export triggers Shapr3D to (re)generate its per-project
tessellation cache (a SQLite database of per-face triangle meshes) via a
`shapr3d://` deep link, then converts those cached meshes into a single binary `.stl`.

> The `.stl` is in millimeters (Shapr3D uses meters) with zero facet normals
> `(0, 0, 0)`. Orientation comes from vertex winding, so the mesh is correct,
> though a few tools that don't recompute normals may shade faces wrong.

Parasolid export selects the final body partitions from the project's workspace
database, recovers their names, and merges them into a single named `.x_b`
assembly using the [`ps-parser`](https://github.com/khoanguyen-3fc/ps-parser)
library (vendored as a git submodule — see below).

## Requirements

- Python 3.10+
- Shapr3D installed with local project data

## Install

This project vendors [`ps-parser`](https://github.com/khoanguyen-3fc/ps-parser)
as a git submodule under `vendor/ps-parser`. Clone with submodules, or
initialize them if you already cloned:

```bash
git clone --recurse-submodules <repo-url>
# or, in an existing checkout:
git submodule update --init
```

Then create the environment and install dependencies (this installs `psparser`
editable from the submodule, per the `-e ./vendor/ps-parser` line in
`requirements.txt`):

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
source venv/bin/activate
python app.py
```

## STL Export Comparison

| Export Method                | Vertices | Triangles |
| ---------------------------- | -------: | --------: |
| Web export trick             |    9,014 |    10,946 |
| **This tool**                |   56,991 |    18,997 |
| Shapr3D High Resolution      |  102,516 |    34,172 |

## License

This project is licensed under the GNU Affero General Public License v3.0 only.
See [LICENSE](LICENSE) for details.

## TODO

- [ ] Windows support
- [ ] Disable Shapr3D Cloud Sync
- [ ] Use theme-matched thumbnails
- [ ] Reconstruct tessellation without opening Shapr3D
- [x] Export Parasolid (`.x_b`)
