# TODO

- Add automated tests around the CoreGraphics event-tap lifecycle; these need
  to run on a macOS host with input monitoring available.
- Verify sleep/wake behavior with a real lid-close cycle on supported macOS
  hardware.
- Consider replacing frontmost-application sampling with native application
  activation notifications if sub-second focus transition accuracy is needed.
- Decide whether CSV rotation or retention limits are needed for long-running
  deployments.
- Add an integration test that waits across a real minute boundary when the
  runtime environment supports reliable wall-clock control.
