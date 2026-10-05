# Diagnostic local — Arty Frame Studio

**Revue du 5 octobre 2026 · Arty A7-100T · Keysight DSOX1202A · Windows natif**

Ce rapport accompagne les corrections locales de l’application. Il distingue les observations rapportées par l’utilisateur, les conclusions permises par les données et les vérifications encore nécessaires sur matériel. Les photographies brutes et les identifiants personnels, séries USB et adresses réseau sont exclus de ce document.

## 1. Résultat du diagnostic

Trois sujets distincts expliquent le parcours observé : l’écran de l’oscilloscope et son acquisition SCPI, la corrélation des réponses UART, et la préparation locale du firmware. Une détection JTAG réussie ne valide ni le dialogue UART ni l’affichage du signal dans l’application.

| Observation rapportée | Ce qu’elle établit | Suite utile |
| --- | --- | --- |
| DATA est visible sur JB1 à l’écran du DSOX1202A. | Un signal est présent sur la sortie sondée au moment de cette capture. | Lire les points et les réglages déjà présents sur l’instrument. |
| L’application indique « Acq. 11 », « Auto forcé », environ 447 ms, puis « Aucune acquisition ». | L’état affiché et le rendu de l’acquisition sont incohérents. | Séparer lecture d’écran, déclenchement et dessin du Canvas. |
| L’axe conservé indique 1 V/div et 50 ns/div ; les contrôles indiquent 10 V/div, offset 15,955 V et 500 ns/div. | Les réglages courants et ceux du tracé conservé ne désignent pas la même capture. | Identifier les axes de chaque capture ; repeindre le tracé reçu. |
| CH2 est désactivée. | Cette capture ne permet pas de mesurer CLK sur CH2. | Activer CH2 pour l’essai de CLK ; préserver son état lors d’une simple lecture. |
| Le JTAG reconnaît le 100T et le journal annonce un chargement SRAM du firmware de référence. | La cible est reconnue et le chargement a été rapporté comme réussi. | Vérifier ensuite PING, INFO et les sorties séparément. |
| PING/INFO expirent alors que des paquets INFO à CRC valide sont reçus. | Des octets structurés arrivent, mais ils ne confirment pas la requête attendue. | Afficher opcode et séquence reçus/attendus ; conserver la corrélation stricte. |

Le correctif d’oscilloscope doit permettre de consulter l’acquisition déjà affichée sans armer SINGLE ni forcer un nouveau déclenchement. Le diagnostic UART doit rendre les réponses décalées compréhensibles ; accepter arbitrairement leur numéro de séquence masquerait le problème.

La compilation Windows locale est préparée avec des outils libres portables dans le compte utilisateur. La compilation GitHub reste disponible. Changer un mot, une fréquence réalisable ou une durée de latch relève du pilotage UART ; changer l’horloge du cœur ou les broches exige un nouveau firmware.

<!-- pagebreak -->

## 2. Oscilloscope : retrouver le signal déjà visible

### Ce qui change dans le parcours

**Connecter / Lire l’écran** relit l’acquisition existante et ses réglages, sans armer SINGLE ni forcer un déclenchement. **Run dans l’application** répète ces lectures : il suit l’état de l’instrument. Un scope arrêté reste arrêté ; sur un scope actif, la lecture stabilise temporairement la trace par STOP, puis reprend RUN. Pour suivre de nouvelles acquisitions, démarrer Run sur la face avant. **Single dans l’application** arme une acquisition unique ; en Auto, un déclenchement forcé éventuel reste explicitement signalé.

Le Canvas dessine les courbes à partir des points effectivement reçus, avec leurs axes. Les calibres lus sont affichés avant le transfert, y compris si celui-ci échoue ; l’erreur reste visible. Un compteur d’acquisitions seul ne prouve pas qu’une courbe a été affichée. L’essai isolé du Canvas sous Flet 0.28.3 confirme son rafraîchissement normal dans le navigateur ; la cause du tracé figé sur le poste Windows n’est pas démontrée. Le curseur choisi au début d’un glissement reste sélectionné jusqu’au relâchement : croiser une autre ligne ne déplace plus les deux curseurs.

### Interpréter les calibres sans mélanger deux captures

Les contrôles peuvent refléter les réglages actuels du DSOX1202A, tandis qu’une capture conservée garde ses anciens axes. C’est une distinction utile uniquement si elle est visible : le lecteur doit savoir à quelle acquisition chaque calibre appartient. Dans le retour fourni, 50 ns/div et 500 ns/div représentent un facteur dix ; 1 V/div et 10 V/div aussi. L’offset 15,955 V est un réglage vertical rapporté, pas une preuve d’une sortie FPGA à cette tension.

Avec CH2 désactivée, une lecture respectueuse de l’écran ne doit ni inventer une courbe CLK ni activer la voie sans action explicite. L’essai de fréquence nécessite ensuite une CH2 active et réellement raccordée à CLK.

### Erreurs historiques à traiter séparément

Les anciens journaux mentionnent une attente dépassée sur le registre d’état d’opération (`OPERation:CONDition?`) et Auto scale. Ces erreurs concernent la commande ou la synchronisation SCPI ; elles ne démontrent pas une mauvaise trame DATA. Auto scale peut modifier calibres, voies et déclenchement : il doit rester une action explicite, avec relecture des réglages obtenus.

Après un délai de transport SCPI, une réponse tardive pourrait contaminer la commande suivante. La session désynchronisée doit être fermée et la reconnexion demandée. Une attente de front en mode Normal est différente : elle peut simplement laisser la dernière capture disponible.

### Vérification concrète sur le DSOX1202A

1. Afficher DATA sur CH1 depuis la face avant ; noter la base de temps et le calibre. Garder CH2 désactivée pour cette première vérification.
2. Connecter l’application, puis utiliser **Lire l’écran**. Vérifier que la courbe et les calibres correspondent à l’écran physique, sans message « Auto forcé » provoqué par cette lecture.
3. Modifier un calibre sur la face avant et relire. Redimensionner la fenêtre : la trace doit rester visible et ses axes cohérents.
4. Activer CH2 et la raccorder à CLK pour la mesure à 10 MHz. Démarrer Run sur l’instrument puis Run dans l’application pour le suivi, ou choisir Single pour une nouvelle acquisition unique.

<!-- pagebreak -->

## 3. UART : une réponse INFO ne confirme pas PING

Le protocole attend la synchronisation `A7 7A`, la version, l’opcode, la séquence, la longueur, la charge utile et un CRC16 CCITT-FALSE. Une réponse doit correspondre à **la fois** à l’opcode et à la séquence de la requête en cours.

Deux paquets fournis pour cette revue sont structurés et leur CRC est valide :

```text
A7 7A 01 86 02 04 00 00 04 00 B3 B2
A7 7A 01 86 01 04 00 00 04 00 53 7C
```

| Champ | Valeur | Interprétation |
| --- | --- | --- |
| Version | 01 | Protocole version 1. |
| Opcode | 86 | Réponse INFO : 06 avec le bit de réponse 80. |
| Séquence | 02 puis 01 | Identifiant repris d’une requête ; doit correspondre à celle en cours. |
| Longueur | 04 | Réponse commune de quatre octets. |
| Charge utile | 00 00 04 00 | Status accepté, busy = 0, mot de page = 4. Compatible avec la révision 4 si la page INFO 0 était demandée. |
| Réponse attendue à PING | 81 et sa séquence | Un paquet 86 ne peut pas confirmer ce PING. |

Ces octets rendent moins probable une liaison totalement muette ou uniquement remplie de texte d’une autre démonstration. Ils ne prouvent pas l’identité complète du firmware : INFO utilise plusieurs pages, et le paquet ne contient pas le numéro de page demandé. L’horloge et l’identifiant de build doivent être lus dans un dialogue correctement corrélé.

### Hypothèses et discriminants

| Hypothèse | Vérification qui permet de progresser |
| --- | --- |
| Réponse d’une requête précédente encore en transit. | Journal horodaté TX/RX indiquant opcode et séquence pour chaque tentative. |
| Une autre application ou un autre échange utilise l’UART. | Fermer les terminaux et outils série ; effectuer une seule connexion et conserver son journal. |
| Réinitialisation, mauvais port, firmware ou état de carte différent de celui supposé. | Vérifier le port courant, la LED PLL et l’alimentation ; recharger le fichier vérifié, puis tester PING avant INFO. |
| Défaut de protocole côté firmware ou de transport côté PC. | Reproduire une transaction isolée et comparer le TX/RX exact ; ne modifier le RTL qu’à partir d’une preuve reproductible. |

Le diagnostic doit compter les paquets hors requête et afficher, pour **chaque commande**, l’opcode et la séquence reçus face à ceux attendus. Une erreur d’entrée/sortie ferme proprement la liaison. Le CRC et la corrélation restent obligatoires. SEND et STOP ne sont pas rejoués automatiquement après un délai dépassé : leur exécution pourrait déjà avoir eu lieu.

<!-- pagebreak -->

## 4. Préparer le firmware localement sous Windows

### Réglages de trame et réglages du FPGA

| Paramètre | Où il s’applique | Recompilation |
| --- | --- | --- |
| Mot, 1 à 26 bits, ordre MSB/LSB. | Payload de SEND. | Non. |
| Fréquence de CLK, latch, pause, répétitions, CLK libre. | Diviseur et ticks de SEND, selon l’horloge et les capacités annoncées par INFO. | Non si la demande est réalisable avec ce firmware. |
| Horloge du cœur, broches DATA/CLK/LATCH, courant et slew. | Configuration FPGA, PLL et contraintes de broches. | Oui. |

Avec le firmware de référence à 200 MHz, **10 MHz utilise un diviseur de 20** et donne une période de **100 ns**. Les durées en mode rafales sont quantifiées à 2,5 ns. En CLK libre, latch et pause sont alignés sur des périodes entières de CLK. La valeur réellement réalisable doit être affichée avant l’envoi.

Le bouton **Préparer le firmware depuis Pilotage** réutilise un firmware compatible ou lance un build local si la configuration matérielle diffère. Les paramètres de fréquence et de broches restent explicites. Modifier la trame n’appelle pas inutilement le compilateur ; modifier l’horloge du cœur, les broches ou les paramètres électriques prépare un build distinct et vérifiable.

### Installation portable dans le compte utilisateur

1. Extraire le projet dans un chemin court, par exemple `C:\ArtyFrameStudio`, hors dossier synchronisé. Installer Python 3.11+ **64 bits** pour son compte ; lancer `start-windows.cmd` dans un terminal ordinaire ou par double-clic.
2. Dans FPGA, cliquer sur **Installer les outils Windows locaux**. Prévoir Internet au premier téléchargement et au moins **4 Go libres**. Les archives représentent environ **447 Mo** au total. La CLI propose aussi `arty-frame install-fpga-tools --project-root .`.
3. L’application télécharge **OSS CAD Suite 2026-03-24** et **openXC7 2026-09-30**, vérifie les tailles et SHA256 épinglés, puis extrait les outils portables.
4. Les outils sont conservés sous `%LOCALAPPDATA%\ArtyFrameStudio\fpga-tools` ; `toolchain.json` référence cette installation. Aucun changement du PATH système, pilote, Linux, WSL ou compte administrateur n’est requis par ce parcours.
5. Cliquer sur **Vérifier les outils**, puis choisir l’horloge du cœur et les broches. Pour le premier essai, garder 200 MHz et JB1/JB2/JB3 ; le firmware fourni suffit si aucune modification matérielle n’est souhaitée.
6. Cliquer sur **Compiler sur ce PC**. La synthèse et le routage doivent réussir, et le timing après routage doit atteindre l’horloge demandée. Le résultat comprend le `.bit`, son manifeste et ses rapports. Les builds sont placés sous `%LOCALAPPDATA%\ArtyFrameStudio\builds`, dans un sous-dossier propre au projet, pour éviter les chemins synchronisés ou trop longs.
7. Charger le résultat via **Charger le .bit sous Windows**, puis reconnecter l’UART pour relire PING et INFO. La compilation seule ne modifie pas la carte.

La compilation GitHub reste une alternative. La préparation locale ne demande pas de jeton GitHub ; le téléchargement des outils requiert une connexion Internet uniquement si le cache vérifié est absent. Une installation incomplète ou un hash incorrect doit arrêter le parcours avec une erreur exploitable.

<!-- pagebreak -->

## 5. Essai complet à 10 MHz

### Installer, charger, dialoguer, envoyer, lire

1. **Installer et préparer.** Lancer l’application mise à jour ; préparer les outils locaux seulement si une compilation est nécessaire. Fermer les autres applications JTAG/UART. Garder les pilotes Digilent/FTDI existants.
2. **Contrôler le fichier.** Conserver ensemble le `.bit`, le manifeste et les rapports. Le firmware de référence cible `xc7a100tcsg324-1`, cœur 200 MHz, DATA JB1, CLK JB2, LATCH JB3, UART 115200 bauds 8N1. Son SHA256 est donné ci-dessous.
3. **Charger la SRAM.** Détecter le JTAG, charger le `.bit`, attendre le résultat et vérifier la LED de verrouillage PLL. La SRAM est volatile : recharger après une coupure. L’IDCODE 100T ne suffit pas à prouver le dialogue applicatif.
4. **Tester PING puis INFO.** Sélectionner le port de la carte, par exemple COM7 dans ce retour, puis Connecter. Confirmer horloge, révision, capacités et identifiant de build. Si un paquet hors requête apparaît, conserver le journal ; ne pas lancer SEND avant une connexion confirmée.
5. **Envoyer une trame simple.** Charger l’exemple SIPO **0xA5, 8 bits, 10 MHz**, LATCH actif haut, CLK en rafales. Utiliser une répétition pour faciliter l’observation, puis arrêter l’émission depuis Pilotage.
6. **Mesurer.** Sondes ×10 avec réglage de voie accordé ; CH1 sur DATA/JB1, CH2 sur CLK/JB2, masse courte sur JB5 ou JB11. Activer CH2. Commencer vers 500 ns/div pour voir plusieurs trames, puis 50–100 ns/div pour les fronts de CLK ; utiliser un calibre vertical adapté aux niveaux 0/3,3 V, par exemple 1 V/div.
7. **Lire l’écran.** Afficher le signal sur l’instrument, utiliser Lire l’écran, comparer la courbe et les axes. Vérifier CLK à environ **10 MHz / 100 ns**. DATA 0xA5 n’a pas nécessairement une fréquence unique ; sa fréquence affichée ne remplace pas la mesure de CLK.

```text
SHA256 du firmware de référence :
02c208aa8cbe79599f605e2f14d667a8f4a77361e56637832449e359f2edb57e
```

### Critères à cocher sur la carte et l’instrument

| Contrôle | Résultat attendu |
| --- | --- |
| Lecture d’écran avec CH2 désactivée. | CH1 visible, CH2 absente, calibres relus, aucun déclenchement forcé par la lecture. |
| Redimensionnement de la fenêtre et deuxième capture. | Trace repeinte, axes liés aux données de la capture. |
| PING et INFO. | Réponses avec CRC, opcode et séquence attendus ; identité complète relue. |
| SEND et STOP. | SEND confirmé, DATA conforme au mot, CLK à 10 MHz ; STOP confirmé et sorties au repos. |
| Build local personnalisé. | Outils vérifiés, timing accepté, bitstream et rapports cohérents, identité après chargement. |

**Stop dans l’onglet Oscilloscope** suspend les lectures du PC ; **Stop sur la face avant** arrête l’acquisition de l’instrument ; **Arrêter dans Pilotage** envoie STOP au FPGA. Déconnecter l’UART ne garantit pas l’arrêt d’une émission continue.

<!-- pagebreak -->

## 6. Validation, limites et références

### État des vérifications de cette revue

Validation locale : **877 tests Python**, **mypy sur 20 fichiers**, **Ruff / formatage** et **six bancs RTL** passent. Le moteur couvre 1 990 906 contrôles ; le banc d’intégration simule l’UART à 200 MHz / 115200 bauds. Le firmware fourni est vérifié ; son RTL et son `.bit` restent inchangés.

La [compilation Windows native](https://github.com/Citroz31/arty-frame-studio/actions/runs/37338522797) a réussi : installation et réutilisation des outils, synthèse/routage depuis un chemin avec espaces, `.bit` à cœur de **150 MHz**, timing de cœur annoncé **189,90 MHz**. L’IDCODE, le SHA256, les sources et le reçu sont vérifiés, puis le fichier téléchargé est relu indépendamment. Cette configuration compile sans WSL ni Vivado ; les sorties physiques restent à mesurer.

Les [contrôles du code publié](https://github.com/Citroz31/arty-frame-studio/actions/runs/37338522961) passent sous Linux et Windows ; premier passage Windows : **803 tests**, **71 cas spécifiques ignorés**. La [compilation Linux de référence](https://github.com/Citroz31/arty-frame-studio/actions/runs/37338522788) passe aussi. L’interface est vérifiée à **1220 × 930** et **760 × 680** : préparation, scope simulé, Run/Single, axes, curseurs et redimensionnement. Le ZIP GitHub correspond aux fichiers validés.

Ce PDF est relu : **six pages A4**, texte Unicode extractible, tableaux vérifiés et marges respectées. Le générateur `scripts/build-local-report.py` utilise ReportLab et DejaVu, sans ajouter de dépendance à l’application.

### Ce qui reste à établir physiquement

- Les corrections SCPI et de rendu doivent être confirmées avec le DSOX1202A réel : lecture de l’écran existant, CH2 désactivée, calibres, Run/Single et délais.
- Le dialogue UART doit réussir avec des réponses corrélées après un chargement du firmware vérifié. Des réponses INFO isolées à CRC valide ne suffisent pas.
- La compilation native Windows doit être distinguée de l’exécution des tests Python Windows et de la compilation Linux existante. Aucun de ces contrôles ne certifie seul la programmation ou les sorties physiques.
- Le timing de référence annoncé dans le manifeste est **210,79 MHz** pour un cœur contraint à 200 MHz. Il couvre les chemins modélisés par nextpnr ; il ne certifie pas les sorties DDR, les câbles Pmod ni les marges du récepteur.
- Le DSOX1202A a 70 MHz de bande passante de base, ou 100/200 MHz selon l’option. La sonde N2140A est spécifiée à 6 MHz en ×1 et 200 MHz en ×10. La chaîne de mesure à 200 MHz nécessite une validation propre ; commencer à 1 MHz puis 10 MHz.

**Aucune Arty ni aucun oscilloscope physique ne sont disponibles dans l’environnement de cette revue.** Les constats matériels ci-dessus proviennent du retour utilisateur ; les changements locaux et leurs tests ne prouvent pas encore la résolution du problème sur le poste réel.

### Sources et mode d’emploi

- [Guide Windows](windows.md), [chaîne FPGA](toolchain.md), [guide utilisateur SIPO/SPI](guide-utilisateur-sipo-spi.md), [guide oscilloscope](oscilloscope.md), [protocole UART](protocol.md).
- [Manifeste du firmware de référence](../firmware/prebuilt/firmware-manifest.json) : hash, configuration, outils, timing et `hardware_validated: false`.
- Keysight, [guide utilisateur 1200 X-Series](https://www.keysight.com/content/dam/keysight/en/doc/gate/user-manuals/9018-70020.pdf) : acquisition Single et interfaces.
- Keysight, [guide de programmation 1200 X-Series](https://www.keysight.com/content/dam/keysight/en/doc/gate/programming-guides/9018-07747.pdf) : SCPI, points visibles et acquisition.
- Keysight, [fiche technique 1000 X-Series](https://www.keysight.com/content/dam/keysight/en/doc/ungate/data-sheets/5992-3484.pdf) et [guide N2140A/N2142A](https://www.keysight.com/us/en/assets/9018-04462/quick-start-guides/9018-04462.pdf) : limites de mesure.

```bash
python scripts/build-local-report.py
# Si nécessaire : --font-dir /chemin/vers/les/polices/DejaVu
```
