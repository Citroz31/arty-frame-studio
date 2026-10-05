# Revue du journal Windows et de l’oscilloscope

Revue du 5 octobre 2026, à partir de `bc7ed5b` sur `main`, qui fusionne les
six commits de Claude (`01bb7f0` à `b79f59c`). Le PDF fourni décrit encore une
branche séparée ; ces ajouts étaient déjà présents dans `main` au début de
cette revue.

## Ce que Claude a apporté

Le correctif du journal évite de fermer une connexion UART fonctionnelle si
`toolchain.json` est absent. Les boutons de compilation locale sont désactivés
avec une explication, et le panneau de résultat peut être masqué. Le parcours
Windows pour charger un `.bit` existant reste disponible sans WSL ni Vivado.
Les indications sur OneDrive et les chemins longs répondent à l’emplacement
signalé dans le compte rendu.

L’onglet Oscilloscope constitue un ajout utile : connexion LAN/USB VISA au
Keysight DSOX1202A, deux voies associables à DATA/CLK/LATCH, acquisition,
calibres, déclenchement, curseurs, CSV et copie d’écran PNG de l’instrument.
Le simulateur permet d’essayer ce parcours sans matériel. Des tests et un
guide avaient également été ajoutés.

## Corrections de cette revue

| Problème observé dans le code | Comportement corrigé |
| --- | --- |
| Une purge réseau de 300 ms après un délai dépassé pouvait laisser une réponse plus tardive contaminer la requête suivante. | Une session LAN/VISA désynchronisée est fermée ; l’interface demande une reconnexion. |
| Une adresse VISA série saisie manuellement contournait le filtre de recherche. | Seules les ressources instrument USB/TCPIP/GPIB prévues sont acceptées ; aucun port COM du FPGA n’est utilisé par ce pilote. |
| L’identité reçue par `*IDN?` était affichée sans vérifier la famille de l’instrument. | L’identité est vérifiée avant configuration ; une fermeture après identité rejetée n’envoie pas `RUN` à un appareil inconnu. |
| `*OPC?` placé après `SINGLE` pouvait attendre le déclenchement et bloquer la surveillance du délai. | Synchronisation avant armement, puis interrogation de l’état d’acquisition. En Auto, un déclenchement forcé permet de voir un niveau continu et est signalé. En Normal, aucun front conserve la dernière capture. |
| Une référence de temps LEFT/RIGHT, une configuration de déclenchement incompatible ou des points non pris en charge pouvaient être mal interprétés. | Référence convertie au centre de l’écran, configurations incompatibles signalées et transfert limité aux nombres de points NORMal documentés. |
| Stop suivi immédiatement de Run pouvait créer deux boucles concurrentes ; une fermeture pouvait abandonner un échange lancé dans un thread. | Une seule boucle à la fois ; fermeture et déconnexion attendent la fin de l’échange SCPI. Les réglages en attente d’une ancienne connexion sont abandonnés. |
| Un réglage refusé, arrondi ou modifié sur l’instrument pouvait laisser les contrôles et le tracé se contredire. | Réglages effectivement relus ; la capture conservée utilise ses propres axes et curseurs, distincts des prochains réglages. |
| Le simulateur répétait une émission finie et utilisait la limite d’aperçu comme période d’une trame longue. | Source analytique respectant longueur, répétitions, pause, CLK libre et niveaux de repos ; aucun budget de dessin ne change le signal simulé. |
| Des impulsions courtes, un front parasite ou l’échantillonnage pouvaient fausser les estimations locales. | Détection et période renforcées, contrôles de cohérence et avertissements sur la résolution. DATA irrégulière ne reçoit pas automatiquement une fréquence périodique locale. |
| Des valeurs de simulation pouvaient porter la mention « mesuré par l’oscilloscope ». | Simulation et capture d’instrument sont identifiées dans les mesures et les exports ; DATA et CLK ont des interprétations distinctes. |
| Le formulaire de connexion occupait une grande partie de la fenêtre une fois connecté. | Détails repliables, identité et actions principales visibles, courbes et mesures rapprochées. Le badge général précise désormais qu’il décrit le FPGA. |
| Les noms d’exports à la seconde pouvaient se remplacer ; certaines erreurs de fichiers échappaient au parcours utilisateur. | Noms distincts et erreurs d’export affichées ; PNG réservé à l’instrument. |
| Le fichier de dépendances figées ne contenait pas PyVISA. | `uv.lock` régénéré et vérifié. Le délai CLI est validé avant connexion ; le nettoyage UART s’exécute même si celui de l’oscilloscope échoue. |

Les tests ajoutés reproduisent notamment des réponses tardives, des délais
VISA, des blocs binaires tronqués, des courses Run/Stop/déconnexion, des
réglages refusés, une émission finie et des cas de mesure difficiles.

## Lecture des commentaires et des mesures

Les manuels du **1200 X-Series** confirment que `SINGLE` attend un vrai
événement de déclenchement, même lorsque le mode choisi auparavant était
Auto. L’application utilise donc explicitement `Force Trigger` en Auto en
l’absence de front ; elle ne prétend pas avoir observé un front dans ce cas.
**Run dans l’application réarme des acquisitions uniques**, avec une pause
entre elles : il ne fournit pas un enregistrement continu sans interruption.

Sur le DSOX1202A, les mesures SCPI de fréquence et période portent sur le
cycle visible le plus proche de la référence de déclenchement. Une DATA
alternée peut afficher **5 MHz** avec CLK à **10 MHz** ; une DATA irrégulière
ne possède pas nécessairement une période. Mesurer CLK directement sur sa
voie pour vérifier la cadence des bits. Le transfert NORMal donne au plus
1000 points : une grande fenêtre temporelle peut masquer des fronts ou
produire une fréquence trompeuse.

L’hypothèse « 66 Vpp signifie une erreur ×10 et une masse longue » du compte
rendu n’est pas un diagnostic démontré. Un facteur ×10 seul transformerait
3,3 Vpp en 33 Vpp. Vérifier le facteur réel de la sonde, celui de la voie,
la masse et la forme d’onde avant de conclure. Une N2140A est donnée pour
6 MHz en ×1 et 200 MHz en ×10 ; les limites dépendent de la sonde exacte.

## Parcours conseillé à 10 MHz

1. Charger le firmware, vérifier PING/INFO, puis utiliser l’exemple SIPO dans
   **Pilotage**. Consulter le [guide utilisateur](guide-utilisateur-sipo-spi.md).
2. Relier CH1 à DATA et CH2 à CLK, avec masses communes et sondes adaptées.
   Faire correspondre les réglages de facteur de sonde à leur commutateur.
3. Dans **Oscilloscope**, choisir LAN si l’appareil possède une prise RJ45,
   ou USB si une bibliothèque VISA est déjà installée. Pour un PC Windows
   sans droits administrateur, LAN évite l’installation de cette bibliothèque.
4. Choisir **Préréglage de la trame**, puis **Run** pour réarmer pendant que
   l’on retourne dans Pilotage lancer l’émission. Pour commencer, une séquence
   répétitive facilite l’observation ; arrêter ensuite le FPGA dans Pilotage.
5. Lire CLK : environ **10 MHz**, **100 ns**. Utiliser les curseurs sur des
   fronts successifs. **Stop de l’oscilloscope arrête les acquisitions du PC** ;
   il ne commande pas le STOP du FPGA.

Le [guide oscilloscope](oscilloscope.md) contient le câblage, les réglages,
le dépannage et les références Keysight.

## Portée de la vérification

**766 tests Python réussissent**, avec Ruff, formatage et mypy. Les **six
bancs RTL** passent, et les empreintes du firmware fourni sont vérifiées.
Une installation neuve par `uv sync --extra dev --frozen` réussit avec PyVISA.
Le rendu complet Flet/Chromium est testé à **1220 × 930** et **760 × 680** :
Run/Stop/reprise, Single, Auto scale, préréglage, connexion repliable, curseurs
sur **100 ns / 10 MHz**, CSV de **1000 points par voie** et déconnexion ; aucun
message d’erreur JavaScript. L’installation Windows et ces vérifications
Python sont également exécutées dans GitHub Actions à chaque publication.

Voir les captures de l’[interface complète](images/oscilloscope-interface.png)
et de la [petite fenêtre](images/oscilloscope-petite-fenetre.png).

Le retour utilisateur rapporte déjà une programmation SRAM, le dialogue
UART, le test LED et DATA sur JB1 à 10 MHz. Nous ne disposons ici d’aucune
Arty ni d’aucun DSOX1202A réel : l’échange SCPI matériel et les sorties à
200 MHz restent à vérifier. Le simulateur illustre le fonctionnement ; il
ne modélise pas toute la bande passante, la sonde et le bruit d’un appareil.
Le RTL et le bitstream de référence ne sont pas modifiés par cette revue.

Sources principales : [guide utilisateur 1200 X-Series](https://www.keysight.com/content/dam/keysight/en/doc/gate/user-manuals/9018-70020.pdf),
[guide programmeur](https://www.keysight.com/content/dam/keysight/en/doc/gate/programming-guides/9018-07747.pdf),
[fiche technique](https://www.keysight.com/content/dam/keysight/en/doc/ungate/data-sheets/5992-3484.pdf).
