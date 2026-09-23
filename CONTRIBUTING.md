# Contributing to MacVitals

MacVitals is a source-available, non-commercial SSH monitor. Contributions are welcome, especially hardware compatibility fixes and parser improvements that can be verified on a real Mac.

## Before opening an issue

Please include:

- macOS version
- chip and Mac model
- the command or page that showed the problem
- a short reproduction path
- relevant, sanitized output from `python3 -m macvitals --diagnose`

Do not include sudo passwords, private keys, tokens, full usernames, process names, command lines, home-directory paths, or unredacted network details.

## Pull requests

For sensor mappings and powermetrics changes, include the evidence that supports the interpretation. Keep unknown values unknown; do not replace unavailable measurements with zero. Preserve the SSH-first terminal workflow, no-history design, and narrow-terminal behavior.

By submitting a contribution, you confirm that you have the right to submit it and agree that it may be distributed under the repository's MacVitals Non-Commercial Share-Alike License. Contributions must not add commercial-use rights or copy code from another project without compatible licensing and attribution.

Third-party changes must not present themselves as official MacVitals releases without permission.
