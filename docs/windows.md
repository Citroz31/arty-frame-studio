# Windows natif : démarrage et diagnostic de l'Arty

L'application et les diagnostics ci-dessous s'exécutent sous Windows avec
votre compte utilisateur, sans droits administrateur et sans changement de
pilote USB. Ils utilisent le pilote FTDI déjà installé.

**COM7 visible confirme l'accès USB/UART, pas la présence de notre firmware.**
Si seul le pilote a été installé, le message
`Délai de réponse dépassé pour PING (séquence 0)` est attendu : le FPGA doit
exécuter le firmware du projet pour répondre. Le dépôt ne fournit actuellement
**aucun `.bit` précompilé et validé sur carte**.

## Démarrer l'application

Placer le dépôt dans un dossier où votre compte peut écrire. Installer
Python **3.11 ou plus récent pour votre utilisateur** s'il manque, puis
double-cliquer sur **`start-windows.cmd`**. Le lanceur crée la `.venv` locale,
installe les dépendances au premier démarrage et ouvre Flet. Un accès Internet
est nécessaire pour cette première installation Python.

Après une mise à jour du dépôt, actualiser les dépendances depuis PowerShell,
dans le dossier du projet :

```powershell
.\start-windows.cmd --setup-only
```

La simulation et le mode « Démo locale » fonctionnent sans firmware chargé.
Pour vérifier la carte, utiliser les diagnostics suivants avant d'envoyer
une trame.

## Lire l'identifiant du FPGA par JTAG

### Adept Runtime et lecture `0xFFFFFFFF`

Le Runtime Digilent Adept, par exemple **2.30.4**, installe des composants
USB/JTAG ; son installation ne charge pas le firmware UART de ce projet.
Le chargeur actuel utilise **FTDI D2XX**, sans appeler directement les DLL
DMGR/DJTG du SDK Adept. Conserver votre installation existante.

Une erreur `IDCODE JTAG 0xFFFFFFFF` après la synchronisation MPSSE signifie
que la liaison FTDI a répondu mais que la lecture TDO reste à 1 : ce n'est
pas un identifiant de FPGA, ni une preuve de panne de la carte ou du pilote.
Une version précédente du projet configurait seulement les quatre lignes
JTAG et omettait les GPIO de commande du chemin Digilent. La version corrigée
configure **les deux groupes GPIO** selon le profil Arty d'openFPGALoader,
`E8/EB` et `00/60`. Le journal affiche ce profil pour identifier la version
utilisée. La correction est testée par simulation ; il faut la vérifier sur carte.

Après mise à jour, fermer l'application puis la relancer avec
`start-windows.cmd`, garder la carte alimentée et branchée sur **USB PROG/UART**,
et fermer les autres logiciels qui accèdent au JTAG, dont Adept. Relancer
la détection FPGA. L'identifiant attendu est `0x03631093` (la révision peut
modifier le premier chiffre). Avec plusieurs interfaces, lister les séries
avec `jtag-devices` et sélectionner celle de l'Arty, canal A.

Si `FFFFFFFF` persiste, vérifier alimentation, câble et série sélectionnée.
Ne modifier aucun pilote uniquement sur la base de ce résultat.
Le programme mentionne alors TDO constant et les vérifications correspondantes.

Le Runtime seul ne garantit pas la présence de l'utilitaire `djtgcfg.exe` ;
aucune commande Adept externe n'est nécessaire dans ce parcours.

Références du profil utilisé :
[Arty → Digilent](https://github.com/trabucayre/openFPGALoader/blob/master/src/board.hpp),
[GPIO du câble Digilent](https://github.com/trabucayre/openFPGALoader/blob/master/src/cable.hpp),
[activation des buffers et multiplexeurs](https://github.com/openocd-org/openocd/blob/master/tcl/interface/ftdi/digilent-hs2.cfg).

### Lancer la détection

Dans l'onglet **FPGA**, section **« JTAG Windows natif »**, cliquer sur
**« Détecter le FPGA sous Windows »**. L'application ouvre le canal **A/JTAG**
du FT2232 avec FTDI D2XX et lit l'IDCODE du FPGA. Le canal **B/UART** reste
celui de COM7.

Le champ **« DLL FTDI D2XX (facultatif) »** peut rester vide : le programme
cherche la DLL du pilote existant. Si elle n'est pas trouvée, fournir son
chemin. La DLL et Python doivent avoir la **même architecture**, par exemple
tous deux 64 bits. Si plusieurs cartes sont raccordées, renseigner la série
du **canal A** dans le champ prévu.

Depuis PowerShell :

```powershell
.\start-windows.cmd --diagnose-jtag
```

Pour lister les interfaces et choisir explicitement la carte :

```powershell
.\.venv\Scripts\arty-frame.exe jtag-devices
.\.venv\Scripts\arty-frame.exe jtag-diagnose --serial "SERIE_CANAL_A"
```

Avec une DLL fournie explicitement, si nécessaire :

```powershell
.\.venv\Scripts\arty-frame.exe jtag-diagnose --ftdi-dll "C:\FTDI\ftd2xx.dll"
```

**Un IDCODE 100T reconnu confirme l'accès JTAG. Ce diagnostic ne programme
pas le FPGA et ne teste pas le firmware UART.** Il ne charge aucun bitstream.
Conserver les pilotes FTDI existants ; ne remplacer aucune interface par
WinUSB avec Zadig, en particulier l'interface **B/UART/VCP** qui fournit COM7.

## Vérifier le firmware sur COM7

Fermer les autres terminaux série et déconnecter la carte dans Flet avant
le diagnostic : COM7 doit être utilisé par un seul logiciel à la fois.

```powershell
.\start-windows.cmd --diagnose-com7
```

La commande équivalente est :

```powershell
.\.venv\Scripts\arty-frame.exe diagnose --port COM7 --timeout 2
```

Elle envoie uniquement **PING**, aucune commande SEND ni trame GPIO. La liaison
utilise **115200 bauds, 8N1, aucun contrôle de flux**. Si PING répond, ouvrir
Flet, choisir **« Carte · USB / UART »**, actualiser les ports, sélectionner
COM7 et cliquer sur **Connecter**.

Si le délai PING expire malgré des octets reçus, la liaison série reçoit des
données mais aucun paquet compatible. Cela peut provenir d'un autre programme,
du mauvais port COM ou d'un débit différent ; le nombre d'octets seul ne permet
pas de choisir la cause. Le journal indique le port testé, les identifiants USB
des ports recensés et les **32 premiers octets reçus au maximum**, en hexadécimal
et ASCII lisible. Les caractères de contrôle sont remplacés dans l'aperçu texte.
Ce diagnostic n'envoie toujours aucune trame GPIO et ne relance pas automatiquement
une commande. Il faut charger **notre firmware UART** pour utiliser le pilotage.

| Résultat | Signification et vérification suivante |
| --- | --- |
| COM7 absent | Vérifier câble, alimentation, numéro COM actuel et pilote FTDI VCP. |
| Port occupé ou accès refusé | Fermer le logiciel qui utilise COM7. |
| DLL D2XX introuvable | Indiquer la DLL du pilote installé ; vérifier l'architecture de Python et de la DLL. |
| IDCODE `FFFFFFFF` ou `00000000` | TDO constant : utiliser la version avec profil Digilent Arty, vérifier alimentation et série JTAG A, puis fermer les autres logiciels JTAG. |
| JTAG reconnaît le 100T, PING expire | L'accès au FPGA fonctionne ; vérifier le chargement du firmware UART du projet. |
| PING expire après un chargement | Vérifier le bon firmware, le port choisi et la première LED monochrome `led[0]`, indicateur de verrouillage PLL de ce firmware. |
| PING répond | Le firmware répond au protocole ; l'envoi de trames devient disponible. |

## Ce qu'il reste pour programmer

Le firmware doit être disponible en `.bit`, puis chargé par JTAG en **SRAM**.
Cette configuration disparaît hors tension ; il faut la recharger après
une coupure. Installer le pilote ou lire l'IDCODE ne réalise pas ce chargement.

Si vous possédez déjà un `.bit` destiné à l'Artix-7 100T csg324, le backend
Windows FTDI D2XX peut le charger avec le pilote existant, sans outil externe
de programmation. Dans l'onglet FPGA, utiliser le champ de fichier `.bit`
et le bouton de chargement Windows, distincts du bouton de détection.
La commande équivalente est :

```powershell
.\.venv\Scripts\arty-frame.exe jtag-program --bitstream "C:\Arty\arty_frame.bit"
```

Comme pour le diagnostic, les options `--serial "SERIE_CANAL_A"` et
`--ftdi-dll "C:\FTDI\ftd2xx.dll"` sont disponibles si nécessaire. Le chargeur
vérifie la cible, l'IDCODE embarqué dans le bitstream et les indicateurs de
configuration, dont DONE. **Ce backend est expérimental : tests simulés
uniquement, aucun essai matériel réalisé ici.** Il ne compile pas le firmware
et ne fournit pas un fichier prêt à charger.

Après chargement de **notre firmware UART**, lancer le diagnostic COM7/PING
ci-dessus avant d'envoyer une trame. Un `.bit` quelconque pour la même puce
peut se charger sans répondre au protocole de l'application.

La couche de compilation peut appeler des outils Windows natifs fournis dans
un dossier utilisateur. Le modèle
[`toolchain.windows.example.json`](../examples/toolchain.windows.example.json)
prévoit leurs chemins ; **il ne contient ni les exécutables, ni la base 100T,
ni un bitstream**. Une chaîne complète de compilation et de programmation
100T sous Windows n'a pas encore été validée dans ce projet. La présence de
Yosys ou d'une distribution OSS CAD Suite ne prouve pas que le backend xc7
et les convertisseurs nécessaires y soient disponibles et compatibles.

Le diagnostic et le chargement JTAG D2XX d'un fichier existant sont distincts
de cette chaîne de compilation. Ils conservent votre pilote FTDI ; aucune
substitution WinUSB/Zadig n'est demandée.
Les exigences de compilation sont détaillées dans [toolchain.md](toolchain.md).
Le raccordement des sorties figure dans [hardware.md](hardware.md) :
DATA sur JB1, CLK sur JB2 et LATCH sur JB3, en logique 3,3 V.
