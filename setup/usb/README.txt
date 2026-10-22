CERAVIS EDGE — USB INSTALLER
============================

What is on this stick
  ceravis-edge-<version>.tar.gz         the CERAVIS software (one release)
  ceravis-edge-<version>.tar.gz.sha256  its fingerprint (checked before installing)
  install-from-usb.sh                   the installer
  ceravis-secrets.env                   YOUR keys (you create it from the .example)

Before you start
  - The Jetson has JetPack 6 installed (SDK Manager) and is switched on.
  - It is connected to the internet (Ethernet cable is best).
  - ceravis-secrets.env on this stick holds the keys (see the .example file).

Install (about 30-45 minutes the first time)
  1. Plug this stick into the Jetson.
  2. Open a terminal on the Jetson (with a monitor: Ctrl+Alt+T; without one:
     ssh ceravis@192.168.55.1 over the USB-C cable).
  3. If the stick did not open by itself (no desktop):
         sudo mkdir -p /mnt/usb && sudo mount /dev/sda1 /mnt/usb
     and use /mnt/usb below instead of /media/$USER/<stick name>.
  4. Run the ONE command:
         sudo bash /media/$USER/<stick name>/CERAVIS/install-from-usb.sh
  5. When it says "done", open in a browser on the same network:
         http://<the address it printed>:8000
     The first screen shows every readiness check and the camera hotspot's
     password. When it says READY, sign in with the care account's email.
  6. Reboot once (sudo reboot) so the desktop is switched off.

If something fails
  The installer stops and says why. Fix that and run the same command again:
  finished steps are kept, nothing is lost.
