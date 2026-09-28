# Third-party notices

The ScadBuddy container image bakes in the third-party OpenSCAD libraries below
(#169). At start the backend copies each one onto the data volume
(`/data/libraries`) when it is not already there; the library's own `LICENSE` file
travels with it.

Keep this file in step with the `Dockerfile`: update it whenever `BOSL2_REF` /
`BOSL2_COMMIT` move or another library is baked into the image.

## BOSL2

- Upstream: https://github.com/BelfrySCAD/BOSL2
- Pinned ref: `v2.0.761` (commit `f47030c41d88d0676bca73be1c6b7ba58564f9dd`)
- License: BSD-2-Clause
- In the image: `/app/libraries/BOSL2/f47030c41d88d0676bca73be1c6b7ba58564f9dd/BOSL2`,
  with its `LICENSE` file

```text
BSD 2-Clause License

Copyright (c) 2017-2019, Revar Desmera
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```
