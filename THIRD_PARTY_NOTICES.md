# Third-party notices

`pdf2pdfa` contains no third-party runtime library or external PDF engine. Some
standards mapping **data** is derived from upstream public resources and is kept
separate from the owned runtime implementation.

## Adobe CMap Resources

Repository: `adobe-type-tools/cmap-resources`

Currently compiled into `pdf2pdfa/native/predefined_cmap_data.py`:

- Adobe-Japan1 `90ms-RKSJ-H` — upstream Git blob `89c83394e93d6568ba031f349b46fd883e76f755`
- Adobe-Japan1 `90ms-RKSJ-V` — upstream Git blob `eeb6a28a93d970d0d74c7000cc185a117aaaddf0`

The Adobe resources are not executed by `pdf2pdfa`. Their mapping records are
compiled into the owned `CodeSpace` / `CIDRange` / `NotDefRange` data model and
are interpreted only by the repository-owned CMap engine.

Adobe license text:

> Copyright 1990-2023 Adobe. All rights reserved.
>
> Redistribution and use in source and binary forms, with or without
> modification, are permitted provided that the following conditions are met:
>
> Redistributions of source code must retain the above copyright notice, this
> list of conditions and the following disclaimer.
>
> Redistributions in binary form must reproduce the above copyright notice,
> this list of conditions and the following disclaimer in the documentation
> and/or other materials provided with the distribution.
>
> Neither the name of Adobe nor the names of its contributors may be used to
> endorse or promote products derived from this software without specific
> prior written permission.
>
> THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
> AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
> IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
> ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
> LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
> CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
> SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
> INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
> CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
> ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
> POSSIBILITY OF SUCH DAMAGE.
