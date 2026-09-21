# Third-party software and assets

The MIT license in this repository covers its original Python, JavaScript, HTML and shell code. It does not replace upstream licenses.

- **[Freedoom](https://freedoom.github.io/)** supplies the freely licensed game data. Setup downloads Phase 2 from the official [0.13.0 release](https://github.com/freedoom/freedoom/releases/tag/v0.13.0). Its BSD-style license is retained in `assets/FREEDOOM-COPYING.txt`; setup also retrieves upstream credits. Freedoom is not proprietary Doom data.
- **[ViZDoom](https://github.com/Farama-Foundation/ViZDoom)** supplies the engine and Python API through the `vizdoom` dependency. Its source and bundled engine components have their own licenses, documented by [upstream](https://github.com/Farama-Foundation/ViZDoom#license). Installing a dependency does not relicense that dependency under MIT. No engine binary or engine source is vendored here.
- **[Vercel AI SDK](https://github.com/vercel/ai)** supplies the experimental evaluation client, under Apache-2.0. The exact Node dependencies and licenses are recorded in `package-lock.json`.
- FastAPI, Uvicorn, Pillow, NumPy, Shapely and python-dotenv are installed dependencies with their own licenses. See the installed distributions and their upstream projects for notices.

This experiment was inspired by Jev's public Doom demonstrations, including [lukaske/jev-doom-agent](https://github.com/lukaske/jev-doom-agent). This release obtains game data directly from Freedoom and does not vendor that demo's source or assets.

Doom, Jev, TypeSafe and Vercel names belong to their respective owners. This project is independent and does not imply endorsement.
