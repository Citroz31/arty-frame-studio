# Firmware Arty A7-100T précompilé

`arty_frame.bit` cible uniquement `xc7a100tcsg324-1`. UART 115200 bauds, 8N1.
Synthèse Yosys ABC9 et routage nextpnr : 210,44 MHz, contrainte 200 MHz passée.
296 tests Python et cinq simulations RTL passent. Aucun essai de ce firmware
sur une carte physique n’a été réalisé ici.

Dans l’application Windows, onglet FPGA, utiliser « Charger le .bit sous
Windows ». Le fichier de ce dossier est proposé automatiquement. Attendre
30 à 60 secondes, vérifier la LED PLL, puis connecter COM7 et vérifier PING.
Le chargement en SRAM disparaît à la coupure d’alimentation. Les pilotes
Adept existants suffisent ; aucun Linux, WSL ou droit administrateur requis.

La procédure complète est dans [le guide Windows](../../docs/windows.md).
Le manifeste donne le SHA256, les versions des outils et les hashes des
sources exactes. Le reçu et les journaux documentent le build local ; leurs
chemins internes ne sont pas des chemins à configurer sur votre PC Windows.
Le rapport de timing ne valide pas physiquement la liaison GPIO/DDR à 200 MHz.
