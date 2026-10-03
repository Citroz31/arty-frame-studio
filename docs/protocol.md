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

SEND correspond au format Python `struct.Struct("<IBHHHHB")` :

| Champ | Type | Valeurs |
| --- | --- | --- |
| word | uint32 | 0 à 2^bit_count−1 |
| bit_count | uint8 | 1 à 26 |
| divider | uint16 | 1 à 65535, fréquence = horloge du cœur / divider |
| latch_ticks | uint16 | 1 à 65535 |
| gap_ticks | uint16 | 0 à 65535 |
| repeat_count | uint16 | 1 à 65535 |
| flags | uint8 | bit 0 : LSB first ; bit 1 : latch actif bas |

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

SEND accepté remet `completed` à zéro et lance une séquence finie. SEND pendant
une émission est rejeté. PING et STATUS restent disponibles ; completed compte
les répétitions dont le latch **et la pause** sont terminés. STOP préserve le
compteur, interrompt la séquence et ramène les sorties au repos. La durée d’un
frame est `((2 × bits + 1) × divider + latch_ticks + gap_ticks) × tick`.

Chronologie à partir du premier bit prêt (origine du chronogramme, indépendante
de la latence de transport UART) : premier front montant à `divider` ticks,
fronts descendants à `2 × divider`, `4 × divider`, etc. DATA change à ces fronts
et revient à 0 au dernier front descendant. LATCH s’active `divider` ticks
après ce dernier front, reste actif `latch_ticks`, puis vient la pause.
CLK est basse entre les rafales ; latch est au niveau inactif choisi.

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
| 0 | révision du firmware (2) |
| 1 | horloge du cœur en Hz, bits 15-0 |
| 2 | horloge du cœur en Hz, bits 31-16 |
| 3 | capacités : bit 0 test LED, bit 1 INFO |
| 4 | identifiant de build, bits 15-0 |
| 5 | identifiant de build, bits 31-16 |

L'identifiant 0 désigne le firmware de référence. Un firmware personnalisé
porte un CRC32 sur 31 bits de sa configuration (horloge, broches, courant,
fronts) ; l'application l'affiche avec la configuration correspondante quand
elle la connaît. `arty-frame info --port COM7` lit ces six pages.
