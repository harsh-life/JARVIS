# Vendored: Rigby2D Rust runtime

`src/` is an unmodified copy of `runtimes/rust/rigby-runtime/src` from
https://github.com/jaisurya-dev-art/rigby2d at commit
`177fd354b8da75104145e7cb173ec9a2228897e8` (Rigby2D 0.4.3, runtime crate 0.2.0),
licensed MIT OR Apache-2.0 (`LICENSE-MIT`, `LICENSE-APACHE`). Only this
`Cargo.toml` is new: upstream inherits its fields from its own workspace.

To update: copy the upstream `src/` over this one, record the new commit here,
then run `cargo test` in `android/rig-native` (it loads the bundled rig) and the
Android unit tests.
