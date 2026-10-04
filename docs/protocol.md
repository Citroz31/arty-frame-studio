# Protocole USB/UART

Port USB de la carte, UART **115200 bauds, 8N1**, sans contrôle de flux.
L’application effectue PING avant d’accepter une connexion. L’interface USB/JTAG
utilisée par openFPGALoader et le canal USB/UART sont distincts.

Chaque paquet commence par `A7 7A`, suivi de `version`, `opcode`, `sequence` et
`longueur` (un octet chacun), de la charge utile et d’un CRC16 sur deux octets.
Le CRC16 CCITT-FALSE couvre **version jusqu’au dernier octet de payload** :
polynôme `0x1021`, initialisation `0xFFFF`, sans réflexion ni XOR final.
CRC et champs multioctets sont transmis **little endian**. Version actuelle : 1.
La charge utile est limitée à 32 octets.

| Opcode | Commande | Charge utile |
| --- | --- | --- |
| 1 | PING | vide |
| 2 | SEND | 14 octets, décrits ci-dessous |
| 3 | STOP | vide |
| 4 | STATUS | vide |
| 5 | LED | 1 octet : bit 7 manuel, bits 3-0 motif LD7..LD4, bits 6-4 nuls |
| 6 | INFO | 1 octet : page 0 à 5 |

LED et INFO existent à partir de la **révision 2** du firmware. Un firmware de
révision 1 répond « opcode inconnu » (status 1) ; l'application le traite
alors comme le firmware historique à 200 MHz, sans test LED.
L'émission continue (`repeat_count` = 0) existe à partir de la **révision 3** ;
une révision 2 refuse ce SEND (status 2) et ne produit aucune trame.
La CLK libre (bit 2 des flags) existe à partir de la **révision 4** ; une
révision 3 refuse ce SEND (status 2).

SEND correspond au format Python `struct.Struct("<IBHHHHB")` :

| Champ | Type | Valeurs |
| --- | --- | --- |
| word | uint32 | 0 à 2^bit_count−1 |
| bit_count | uint8 | 1 à 26 |
| divider | uint16 | 1 à 65535, fréquence = horloge du cœur / divider |
| latch_ticks | uint16 | 1 à 65535 |
| gap_ticks | uint16 | 0 à 65535 |
| repeat_count | uint16 | 1 à 65535 trames, ou **0 : continu jusqu'à STOP** |
| flags | uint8 | bit 0 : LSB first ; bit 1 : latch actif bas ; bit 2 : CLK libre |

Un tick est un demi-cycle de l'horloge du cœur : **2,5 ns** pour le firmware
de référence à 200 MHz, 3,33 ns à 150 MHz, 5 ns à 100 MHz. Le paquet SEND ne
contient pas cette horloge : l'application la lit par INFO et refuse d'envoyer
une trame calculée pour une autre horloge.
Les autres bits de flags sont interdits. Aucun argument GPIO n’est transmis :
le brochage est fixé à la compilation, dans le XDC.

La réponse reprend `sequence`, utilise `opcode | 0x80`, et porte la charge utile
`<BBH>` : `status`, `busy` (0 ou 1), `completed` (uint16).

| Status | Sens |
| --- | --- |
| 0 | accepté |
| 1 | opcode inconnu |
| 2 | longueur ou paramètres invalides |
| 3 | émission déjà en cours |
| 4 | CRC invalide |
| 5 | version incompatible |

SEND accepté remet `completed` à zéro et lance la séquence : `repeat_count`
trames, ou une répétition sans fin si `repeat_count` vaut 0. SEND pendant
une émission est rejeté. PING et STATUS restent disponibles ; completed compte
les répétitions dont le latch **et la pause** sont terminés. STOP préserve le
compteur, interrompt la séquence et ramène les sorties au repos. La durée d’un
frame est `((2 × bits + 1) × divider + latch_ticks + gap_ticks) × tick`.

En **émission continue**, `busy` reste à 1 jusqu'à STOP (ou un reset/une
coupure) : seule une commande STOP termine CLK, DATA et LATCH. `completed`
compte alors modulo 65 536 et reboucle sans arrêter l'émission ; sa variation
entre deux STATUS témoigne de l'activité, pas du total depuis SEND. Fermer le
port série n'arrête pas la carte. La trame garde sa structure : CLK pulse
`bit_count` fois, puis s'arrête pendant LATCH et la pause, sauf en CLK libre.

### CLK libre (flags bit 2, révision 4)

CLK garde sa période `2 × divider` ticks de SEND jusqu'à la fin ou STOP, y
compris pendant LATCH et la pause. Pour que chaque trame commence en phase
avec CLK, la trame doit durer un nombre entier de périodes :

- LATCH devient actif **au dernier front descendant** des bits (et non une
  demi-période plus tard) et dure `divider + latch_ticks` ticks : l'hôte
  envoie `latch_ticks = (2k − 1) × divider` pour un LATCH de k périodes ;
- la pause vaut `gap_ticks = 2m × divider` (m périodes, éventuellement 0) ;
- la trame dure `2 × divider × (bits + k + m)` ticks ; DATA et LATCH ne
  changent qu'aux fronts descendants de CLK.

Le firmware ne vérifie pas cet alignement : l'application arrondit LATCH et
pause aux périodes entières et `FrameConfig` refuse toute autre valeur. Un
client UART indépendant doit aussi respecter ces contraintes : un SEND
mal aligné peut être accepté tout en produisant des changements de DATA/LATCH
qui ne coïncident plus avec les fronts descendants de CLK.

Un récepteur qui décale à chaque front montant reçoit des zéros supplémentaires
pendant LATCH et la pause. Pour un SIPO à registre de sortie déclenché par un
**front montant** de LATCH, comme le 74HC595, choisir **LATCH actif haut** :
ce front intervient juste après le dernier bit utile, avant les zéros suivants.
La sortie mémorisée reste alors correcte, même si le registre de décalage
continue de changer. Avec LATCH actif bas, le front montant intervient à la
fin du LATCH : les horloges supplémentaires ont déjà décalé des zéros, donc
un tel composant peut mémoriser une autre valeur. Un latch transparent pendant
son niveau actif peut également suivre ces changements. La polarité doit
correspondre au fonctionnement réel du récepteur, pas seulement à son nom.

### Chronologie et distinction avec SPI

L'origine du chronogramme est le premier bit prêt, indépendamment de la
latence du transport UART. Avec `N = divider` et `b = bit_count`, les fronts
montants utiles arrivent aux ticks `N`, `3N`, …, `(2b − 1)N` ; DATA change
aux fronts descendants et revient à zéro au tick `2bN`.

| Mode | Début de LATCH actif | Durée active de LATCH | CLK après les bits |
| --- | --- | --- | --- |
| CLK en rafales (`free_clock = false`) | `(2b + 1)N` | `latch_ticks` | Basse pendant LATCH et la pause |
| CLK libre (`free_clock = true`) | `2bN` | `N + latch_ticks = 2kN` | Continue, y compris pendant LATCH et la pause |

La pause suit la fin de LATCH dans les deux modes. En CLK libre, ses `2mN`
ticks et le LATCH de `2kN` ticks assurent que la trame suivante commence
sur un front descendant, après `b + k + m` périodes de CLK. Dans le mode
en rafales, les durées de LATCH/pause sont simplement des ticks matériels.

DATA correspond à MOSI et CLK à SCLK pour un récepteur qui lit au front montant
(timing de type SPI mode 0 pendant les bits). **LATCH est un signal de
validation après les bits ; ce n'est pas un chip select SPI.** Le moteur
actuel ne fournit pas de CS actif avant le premier bit et maintenu pendant
la transaction, ni MISO, ni sélection générale CPOL/CPHA. Un composant SPI
qui exige CS doit disposer d'un autre moyen adapté pour le gérer ; ne pas
raccorder LATCH à CS en supposant qu'ils sont équivalents.

STOP interrompt immédiatement le moteur, sans attendre la fin du mot ou
d'une période de CLK : le dernier bit, le dernier LATCH ou la dernière
impulsion peuvent être incomplets. Après la latence du pipeline de sortie,
CLK/DATA reviennent à zéro et LATCH au niveau inactif choisi. Un reset force
toutes les sorties à zéro, y compris un LATCH configuré actif bas.

Une seule requête est en vol côté Python, avec vérification du CRC, de l’opcode
et du numéro de séquence. Le firmware reçoit des paquets bornés et abandonne
un paquet incomplet après son timeout. Il n’offre pas de déduplication persistante :
une commande SEND ou STOP n’est **jamais répétée automatiquement** après un
timeout, car elle peut avoir été exécutée. PING (à la connexion), STATUS et
INFO, sans effet sur la carte, et LED, dont la répétition donne le même état,
sont redemandés une fois avec un nouveau numéro de séquence. En cas d'échec
des deux tentatives, les diagnostics conservent les octets et le premier
aperçu RX reçus, même si la deuxième tentative est muette.

L'interface demande STATUS immédiatement après un timeout SEND ou STOP.
Le compteur et le champ busy ne permettent pas de savoir avec certitude si
la commande sans réponse a été exécutée. L'interface conserve cette incertitude
et désactive SEND jusqu'à un STOP confirmé ou une reconnexion explicite.

## Test des LED (opcode 5)

Avec le bit 7 à 1, le motif remplace l'affichage d'état des quatre LED vertes
LD4 à LD7 (bit 0 = LD4) pendant **3 secondes**, quelle que soit l'horloge du
cœur ; chaque nouvelle commande relance ce délai. Avec l'octet `00`, l'état
revient immédiatement. Le reset revient aussi à l'état. La réponse est la
réponse commune (busy, completed). La commande n'a aucun effet sur DATA, CLK,
LATCH ni sur une émission en cours.

L'application fait défiler un chenillard de dix motifs confirmés, puis rend
l'état : l'utilisateur voit que le bon `.bit` tourne sur cette carte, et chaque
réponse confirme le chemin retour. `arty-frame led-test --port COM7` fait de
même en ligne de commande.

## Identification (opcode 6)

La réponse INFO est la réponse commune ; son champ `completed` porte le mot
de 16 bits de la page demandée :

| Page | Contenu |
| --- | --- |
| 0 | révision du firmware (4) |
| 1 | horloge du cœur en Hz, bits 15-0 |
| 2 | horloge du cœur en Hz, bits 31-16 |
| 3 | capacités : bit 0 test LED, bit 1 INFO, bit 2 émission continue, bit 3 CLK libre |
| 4 | identifiant de build, bits 15-0 |
| 5 | identifiant de build, bits 31-16 |

L'identifiant 0 désigne le firmware de référence. Un firmware personnalisé
porte un CRC32 sur 31 bits de sa configuration (horloge, broches, courant,
fronts) ; l'application l'affiche avec la configuration correspondante quand
elle la connaît. `arty-frame info --port COM7` lit ces six pages.
