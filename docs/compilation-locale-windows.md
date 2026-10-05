# Compilation locale sous Windows

Windows x64 et Python 64 bits ≥ 3.11 suffisent pour préparer la chaîne de
compilation du XC7A100T de l’Arty A7-100T. Linux, WSL, Vivado et les droits
administrateur ne sont pas nécessaires. La compilation reste sur le PC.

Dans l’application, utilisez **Installer les outils Windows locaux**. La
préparation télécharge les archives puis crée `toolchain.json` dans le projet.
Le bouton **Préparer le firmware** de Pilotage utilise ensuite cette
configuration pour produire `arty_frame.bit` à partir des paramètres
sélectionnés. Gardez l’application ouverte pour consulter le journal de
synthèse, de placement et de routage.

En PowerShell, depuis le dossier du projet, la même installation est disponible
avec l’interpréteur de l’application :

```powershell
.\.venv\Scripts\python.exe -m arty_frame_studio.cli install-fpga-tools
.\.venv\Scripts\python.exe -m arty_frame_studio.cli doctor --toolchain toolchain.json
.\.venv\Scripts\python.exe -m arty_frame_studio.cli build --toolchain toolchain.json
```

Le script autonome `scripts/bootstrap-windows.py` expose aussi cette préparation
avec `--tools-dir C:\FPGA\arty-tools` et `--config C:\FPGA\arty-toolchain.json`.
Il peut être lancé avec `py -3 scripts\bootstrap-windows.py` si l’application
n’est pas encore installée ; il utilise directement le code du projet. Avec un
fichier JSON personnalisé, passez le même chemin à `doctor` et `build`. Si le
chemin contient des espaces, entourez-le de guillemets.

Les outils sont placés par défaut sous
`%LOCALAPPDATA%\ArtyFrameStudio\fpga-tools`, pour éviter la synchronisation
OneDrive. Les fichiers de compilation et le `.bit` sont placés sous
`%LOCALAPPDATA%\ArtyFrameStudio\builds\<identifiant-du-projet>` ; le JSON donne
leur chemin exact. Chaque projet utilise un dossier distinct, même quand son
chemin contient des espaces. Avec `--tools-dir`, les builds se trouvent dans un
dossier `builds` voisin du dossier des outils.

Prévoyez au moins **4 Go libres** pour la préparation, environ **447 Mo à
télécharger**, et plusieurs minutes pour la compilation. Les archives sont
conservées dans `downloads` et réutilisées après vérification SHA256. Une
installation déjà complète ne télécharge rien.

Les versions sont fixées dans `local_tools.py` :

| Archive | Version | SHA256 |
| --- | --- | --- |
| OSS CAD Suite Windows x64 | 2026-03-24 | `111238a7e52892c561f23bb1e19c197f750a09688aa12787ead7a407e0750476` |
| openXC7 Windows amd64 | 2026-09-30 | `4592f26732360d78124f933fb7e524545cf00954f0c24da26ec36b84a170fac6` |

La suite OSS fournit Yosys, ABC, openFPGALoader et Python 3.11 dans
`oss-cad-suite/lib/python3.exe`. openXC7 fournit nextpnr, `xc7frames2bit`, la
base Artix-7, la chipdb XC7A100T et le script `libexec/fasm2frames`. Ses modules
FASM, Project X-Ray et textX sont du Python pur : le nom du dossier
`lib/python3.12/site-packages` n’exige pas un Python 3.12 supplémentaire.

Le fichier JSON généré ajoute `oss-cad-suite/bin`, `oss-cad-suite/lib` et
`openxc7/bin` au `PATH` des processus de compilation, ainsi que les modules
openXC7 au `PYTHONPATH`. Les variables système et le registre Windows ne sont
pas modifiés. L’interpréteur embarqué ignore `PYTHONHOME` et les modules
utilisateur pour éviter les conflits avec un Python déjà installé.

La préparation vérifie les fichiers, les métadonnées de version, le démarrage
de Yosys/nextpnr, le parseur FASM et l’aide du convertisseur. `toolchain.json`
est remplacé uniquement après ces vérifications. Un échec de téléchargement,
d’extraction ou de vérification conserve le fichier de configuration existant.
L’extraction openXC7 refuse les chemins sortants, les liens et les fichiers
spéciaux. L’auto-extracteur portable OSS est lancé uniquement après vérification
de sa taille et de son SHA256.

La configuration générée utilise le placement **seed 8** avec une marge de
timing de **3 %**. Chaque compilation doit réussir ses propres vérifications
de timing avant de créer le reçu de programmation. Un binaire issu d’une
compilation échouée ne devient pas programmable par ce flux.

Pour programmer la carte, utilisez le résultat `.bit` via le flux Windows de
l’application. La préparation des outils ne change aucun pilote USB. La
programmation SRAM est volatile et doit être répétée après une coupure
d’alimentation de la carte.

En cas d’échec, consultez le journal. Une archive interrompue ou incorrecte est
rejetée ; relancez la préparation après correction de la connexion. Un dossier
de version incomplet n’est pas écrasé : déplacez-le avant de relancer. Après un
arrêt brutal, ne retirez `.install.lock` que si aucune préparation ne tourne.

Les archives exactes et leurs chemins ont été inspectés, et les tests unitaires
contrôlent l’installation et ses échecs. La validation de compilation native
Windows est distincte : le workflow
[`windows-firmware.yml`](../.github/workflows/windows-firmware.yml) installe et
réutilise les outils, puis compile un firmware à 150 MHz depuis un chemin avec
des espaces. Il contrôle le `.bit`, le reçu de compilation et le timing avant
de conserver le firmware, ses métadonnées et les journaux. Consultez le
résultat de ce workflow avant de considérer ce parcours comme validé sur une
machine Windows.
