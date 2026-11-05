//! The JARVIS character player's native library: the Rigby2D runtime behind a small JNI surface
//! (`jni`), with the playback and geometry logic in `rig` so it is tested without a JVM.
pub mod jni;
pub mod rig;
