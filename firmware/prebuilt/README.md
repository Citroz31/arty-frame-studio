# Firmware Arty A7-100T précompilé et corrigé

Ce `arty_frame.bit` remplace la version initiale aux broches UART inversées.
**RX = A9, TX = D10**. Les six entrées des ODDR sont enregistrées ; leur reset
asynchrone R→SR a été contrôlé dans le netlist routé.

Le fichier provient du [build GitHub Actions réussi](https://github.com/Citroz31/arty-frame-studio/actions/runs/37113537280)
du commit `7fa89e95099d3db9968eb42c0e77aa4ec8a2fccf`. Les hashes de toutes les
sources `.v` et `.xdc` du firmware correspondent à ce build. Les changements
ultérieurs de l'application Python ne nécessitent pas de recompiler ce RTL.

- Cible : `xc7a100tcsg324-1`, IDCODE `0x03631093` (révision ignorée).
- UART : 115200 bauds, 8N1.
- Synthèse Yosys ABC9 ; routage nextpnr à 200 MHz, Fmax **218,05 MHz**.
- SHA256 : `4d4f8831154475354e5385581bd08ab296dcdd16b1d4f683ab8f52a51cf4975d`.
- Cinq simulations RTL réussies ; **aucun essai sur carte physique ici**.

Dans l'application Windows, onglet FPGA, utiliser **Charger le .bit sous
Windows**. Le fichier est présélectionné. Attendre 30 à 60 secondes, vérifier
LED0, puis connecter COM7 et vérifier PING. SRAM volatile : recharger après
coupure d'alimentation. Garder les pilotes Adept existants ; aucun Linux, WSL
ou droit administrateur nécessaire sur le PC.

Le chargement contrôle le `.bit`, son manifeste, les sources et le timing du
cœur. Le fichier initial connu est refusé même après renommage. La commande
`arty-frame firmware-check` et les tests CI vérifient la même cohérence.

Conserver ensemble `.bit`, manifeste, `timing.json` et rapports. Les chemins
internes du reçu/journal correspondent au runner GitHub ; ils ne sont pas à
configurer sur Windows. Le Fmax du cœur ne certifie pas les sorties DDR ni la
liaison physique Pmod à 200 MHz. Voir [le guide Windows](../../docs/windows.md).
