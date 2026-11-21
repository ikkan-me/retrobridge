# Sécurité

## Signaler une vulnérabilité

Merci de **ne pas ouvrir d'issue publique** pour une faille de sécurité.
Utilisez l'onglet *Security → Report a vulnerability* du dépôt GitHub, ou
contactez le mainteneur en privé.

Précisez si possible :

- la version concernée (`retrobridge --version`) ;
- les étapes de reproduction ;
- l'impact estimé (lecture/écriture de fichiers, exécution de commandes, fuite
  de secrets RPC, etc.).

## Périmètre

RetroBridge s'exécute localement avec les droits de l'utilisateur et manipule
des chemins fournis dans sa configuration. Les points sensibles sont :

- le mot de passe RPC Transmission, lu depuis `RETROBRIDGE_RPC_PASSWORD` ;
- les conversions externes (`chdman`, `dolphin-tool`) ;
- les chemins de bibliothèque, de téléchargement et de montage SD.
