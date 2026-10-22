# Repository guidance

When adding or packaging a script in this repository:

- Inventory its Python imports, external commands, runtime services, and platform requirements. Declare Python packages in the relevant package metadata; document external commands and verify them at runtime with an actionable error. Include the script in wheel and source distributions, and its tests in the source distribution.
- Replace author-specific absolute paths with paths derived from the current user, script location, installation location, or explicit arguments. Do not embed a developer's home directory or assume a checkout layout in installed tools. Test the installed form as well as the source form.
- Never copy API keys, passwords, tokens, private configuration, session dumps, or logs into source, fixtures, documentation, or release artifacts. Read credentials from the owning tool's normal configuration or a documented runtime input. Do not print command output or exceptions that may contain secrets; keep operational logs private and scan build artifacts before release.
