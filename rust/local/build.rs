// Copyright (c) 2026 OceanBase.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

use std::{env, path::PathBuf};

fn main() {
    let include = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap()).join("include");
    cxx_build::CFG.exported_header_dirs.push(&include);
    cxx_build::bridge("src/bridge.rs")
        .include("include")
        .std("c++17")
        .compile("local_cxx");
    println!("cargo:rerun-if-changed=src/bridge.rs");
    println!("cargo:rerun-if-changed=include/callbacks.h");
}
