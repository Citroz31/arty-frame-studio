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

SEND correspond au format Python `struct.Struct("<IBHHHHB")` :

| Champ | Type | Valeurs |
| --- | --- | --- |
| word | uint32 | 0 à 2^bit_count−1 |
| bit_count | uint8 | 1 à 26 |
| divider | uint16 | 1 à 65535, fréquence = 200 MHz / divider |
| latch_ticks | uint16 | 1 à 65535 |
| gap_ticks | uint16 | 0 à 65535 |
| repeat_count | uint16 | 1 à 65535 |
| flags | uint8 | bit 0 : LSB first ; bit 1 : latch actif bas |

Un tick est un demi-cycle du domaine 200 MHz, soit **2,5 ns**.
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
frame est `((2 × bits + 1) × divider + latch_ticks + gap_ticks) × 2,5 ns`.

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
timeout, car elle peut avoir été exécutée. PING (à la connexion) et STATUS,
sans effet sur la carte, sont redemandés une fois avec un nouveau numéro de
séquence. Consulter STATUS ou envoyer STOP avant de
décider d’un nouvel envoi.
