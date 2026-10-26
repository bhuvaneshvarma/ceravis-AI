CERAVIS EDGE — RELEASE FOLDER
=============================

What is in this folder
  install.sh                            the one command (run on the device)
  ceravis-edge-<version>.tar.gz         the CERAVIS software, AI models included
  ceravis-edge-<version>.tar.gz.sha256  its fingerprint (checked before installing)
  ceravis-secrets.env                   the keys      } added from the PC's ~/.ceravis
  authorized_keys                       developer SSH }  at delivery, never kept in
                                        public keys   }  the project folder

Before you start
  - The Jetson has JetPack 6 installed (SDK Manager) and is switched on.
  - An Ethernet cable runs from a LAN port of the home router to the Jetson
    (the internet comes by cable: the Jetson's Wi-Fi becomes the cameras'
    hotspot). The router gives it an address by itself.

Install over the network — no monitor (from the PC, in Git Bash)
  bash setup/send_to_device.sh ceravis@<device-ip>
      copies this folder with the keys to the device and starts the install;
      the device's address: the router's client list, or 192.168.55.1 over the
      USB-C cable. If the window closes, the install keeps going:
  bash setup/send_to_device.sh ceravis@<device-ip> --follow     watch it again
  bash setup/send_to_device.sh ceravis@<device-ip> --status     done / failed

Install from a USB stick (with a monitor and keyboard, or over SSH)
  1. Plug the stick into the Jetson.
  2. If it did not open by itself:
         sudo mkdir -p /mnt/usb && sudo mount /dev/sda1 /mnt/usb
  3. Run:  sudo bash /media/$USER/<stick name>/CERAVIS/install.sh
     (or /mnt/usb/CERAVIS/install.sh)

Either way (about 30-45 minutes the first time)
  - The install runs as a background service: a closed window or a dropped
    connection does not stop it. Progress any time:
        sudo bash <folder>/install.sh --follow
        sudo tail -f /var/log/ceravis-install.log
  - When it says done, open in a browser on the same network:
        https://<the address it printed>:8443
    The browser asks once to trust the device's own certificate. The first
    screen shows every readiness check and the camera hotspot's password;
    when it says READY, sign in with the care account's email.
  - Reboot once (sudo reboot) so the desktop is switched off.
  - A copy of this folder on the device (sent over the network) is deleted
    after a successful install, keys included. A USB stick is left as it is:
    keep it somewhere safe — it holds the keys.

If something fails
  The installer stops and says why (also in /var/log/ceravis-install.log). Fix
  that and run the same command again: finished steps are kept.
