// docs/23 §5.3: the ONLY privileged surface this client has — one typed call
// per Shizuku-backed primitive in the shared mapping. No argv, no command
// string, no shell: nothing a model (or anyone) can use to pick a command.
package com.hypermind.jarvis.privileged;

interface IJarvisPrivileged {
    // Shizuku's user-service teardown transaction (fixed by Shizuku).
    void destroy() = 16777114;

    // shizuku.force_stop_package — force-stop exactly this package for this user.
    boolean forceStopPackage(String packageName, int userId) = 1;
}
