# License

evcc-calendar is released under the MIT License.

```
MIT License

Copyright (c) 2026 Axel Zehden

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Third-party components

All of them are permissive licenses and compatible with the MIT License.

| Component | Used for | License | Included in repo / image |
|---|---|---|---|
| [PyYAML](https://github.com/yaml/pyyaml) 6.x | reading `config.yaml` | MIT | installed via `requirements.txt` |
| [tzdata](https://github.com/python/tzdata) | time zone data | Apache-2.0 | installed via `requirements.txt` |
| [Bricolage Grotesque](https://github.com/ateliertriay/bricolage) 5.3.0 (via npm `@fontsource-variable/bricolage-grotesque`, latin subset) | UI font | SIL Open Font License 1.1 | `evccplan/static/fonts/bricolage.woff2`, license text in `evccplan/static/fonts/OFL-Bricolage-Grotesque.txt` |
| [pytest](https://github.com/pytest-dev/pytest) and its dependencies (iniconfig, pluggy: MIT; packaging: Apache-2.0 OR BSD-2-Clause; Pygments: BSD-2-Clause) | tests only | MIT and others | `requirements-dev.txt`, not part of the Docker image |
| Python 3.12 (`python:3.12-slim` Docker base image) | runtime | Python Software Foundation License; the image also contains Debian packages under their own licenses | pulled at build time, not stored in the repo |

The font is bundled unmodified in its original WOFF2 form and may be redistributed under the OFL together with its license text; it must not be sold on its own.

## External services (not linked, not bundled)

evcc (MIT), Home Assistant and OpenRouteService are only contacted over their APIs at runtime. Their own terms of use apply to your use of them, in particular the OpenRouteService API terms and its attribution requirements.

## Images

`evccplan/static/img/car.webp` and `hero.webp` were created with an AI image generator (Google Gemini) for this project and are published under the MIT License together with the code. Check the current terms of the generator you use before relying on this for commercial use.
