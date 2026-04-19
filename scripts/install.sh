#!/usr/bin/env bash

# ============================================
# Ari - Assistant Vocal IA Local
# Script d'installation automatique
# ============================================
# Usage: ./scripts/install.sh [--dev] [--no-venv]
# Options:
#   --dev       Installe les dépendances de développement (pytest, black, etc.)
#   --no-venv   Ne crée pas d'environnement virtuel (utilise Python global)
# ============================================

set -e  # Arrêt à la première erreur

# Couleurs pour l'affichage
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Fonction d'affichage
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCÈS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[ATTENTION]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERREUR]${NC} $1"
}

# Banner
echo "============================================"
echo "    Ari - Assistant Vocal IA Local"
echo "    Script d'installation"
echo "============================================"
echo ""

# Variables
CREATE_VENV=true
INSTALL_DEV=false
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"

# Traitement des arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --dev)
            INSTALL_DEV=true
            shift
            ;;
        --no-venv)
            CREATE_VENV=false
            shift
            ;;
        *)
            log_error "Option inconnue: $1"
            echo "Usage: $0 [--dev] [--no-venv]"
            exit 1
            ;;
    esac
done

# 1. Vérification Python
log_info "Vérification de Python 3.12+..."

if ! command -v python3 &> /dev/null; then
    log_error "Python 3 n'est pas installé. Veuillez l'installer avant de continuer."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
REQUIRED_VERSION="3.12"

# Comparaison version
PYTHON_MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
PYTHON_MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)
REQ_MAJOR=$(echo $REQUIRED_VERSION | cut -d. -f1)
REQ_MINOR=$(echo $REQUIRED_VERSION | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt "$REQ_MAJOR" ] || ([ "$PYTHON_MAJOR" -eq "$REQ_MAJOR" ] && [ "$PYTHON_MINOR" -lt "$REQ_MINOR" ]); then
    log_error "Python $PYTHON_VERSION détecté, mais Python 3.12+ est requis."
    log_info "Téléchargez Python depuis https://www.python.org/downloads/"
    exit 1
fi

log_success "Python $PYTHON_VERSION détecté ✓"

# 2. Environnement virtuel (optionnel)
if [ "$CREATE_VENV" = true ]; then
    log_info "Création de l'environnement virtuel..."

    if [ -d "$VENV_DIR" ]; then
        log_warning "L'environnement virtuel existe déjà. Suppression..."
        rm -rf "$VENV_DIR"
    fi

    python3 -m venv "$VENV_DIR"
    log_success "Environnement virtuel créé ✓"

    # Activation
    source "$VENV_DIR/bin/activate"
    PIP_CMD="pip"
else
    log_warning "Création d'environnement virtuel désactivée."
    log_info "Utilisation de Python global."
    PIP_CMD="pip3"
fi

# 3. Mise à jour pip, setuptools, wheel
log_info "Mise à jour de pip, setuptools et wheel..."
$PIP_CMD install --upgrade pip setuptools wheel

# 4. Installation des dépendances
log_info "Installation des dépendances principales..."

if [ -f "${PROJECT_DIR}/requirements.txt" ]; then
    $PIP_CMD install -r "${PROJECT_DIR}/requirements.txt"
    log_success "Dépendances installées ✓"
else
    log_error "Fichier requirements.txt non trouvé dans ${PROJECT_DIR}"
    exit 1
fi

# 5. Installation des dépendances de développement (optionnel)
if [ "$INSTALL_DEV" = true ]; then
    log_info "Installation des dépendances de développement..."
    $PIP_CMD install pytest pytest-asyncio black isort flake8 mypy pre-commit
    log_success "Dépendances de développement installées ✓"
fi

# 6. Vérification Ollama (optionnel mais recommandé)
log_info "Vérification d'Ollama (recommandé)..."
if ! command -v ollama &> /dev/null; then
    log_warning "Ollama n'est pas installé ou pas dans le PATH."
    log_info "  Pour installer Ollama : curl -fsSL https://ollama.com/install.sh | sh"
    log_info "  Puis : ollama pull llama3.2"
else
    log_success "Ollama détecté ✓"
    if ! curl -s http://localhost:11434/api/tags &> /dev/null; then
        log_warning "Ollama n'est pas en cours d'exécution."
        log_info "  Démarrez-le avec : ollama serve"
    else
        log_success "Ollama est en cours d'exécution ✓"
    fi
fi

# 7. Création des directories nécessaires
log_info "Création des répertoires nécessaires..."

DIRS=(
    "${PROJECT_DIR}/memory_profiles"
    "${PROJECT_DIR}/logs"
    "${PROJECT_DIR}/data"
    "${PROJECT_DIR}/models"
)

for dir in "${DIRS[@]}"; do
    mkdir -p "$dir"
    log_success "Répertoire créé: $dir"
done

# 8. Permissions
log_info "Application des permissions..."

# Rendre les scripts exécutables
chmod +x "${PROJECT_DIR}/scripts/install.sh"
chmod +x "${PROJECT_DIR}/scripts/run.sh"

# Permissions logs (écriture)
chmod 755 "${PROJECT_DIR}/logs"

# 9. Copie du fichier .env si absent
if [ ! -f "${PROJECT_DIR}/.env" ]; then
    log_info "Création du fichier .env à partir de .env.example..."
    if [ -f "${PROJECT_DIR}/.env.example" ]; then
        cp "${PROJECT_DIR}/.env.example" "${PROJECT_DIR}/.env"
        log_success "Fichier .env créé ✓"
        log_warning "N'oubliez pas de modifier .env avec vos paramètres !"
    else
        log_error "Fichier .env.example non trouvé."
    fi
else
    log_warning "Le fichier .env existe déjà. Aucune modification."
fi

# 10. Récapitulatif
echo ""
echo "============================================"
echo "    Installation terminée !"
echo "============================================"
echo ""
log_info "Prochaines étapes :"
echo ""
echo "  1. Éditer le fichier .env :"
echo "     nano .env  # ou votre éditeur"
echo ""
echo "  2. Démarrer Ollama (si pas déjà fait) :"
echo "     ollama serve"
echo ""
echo "  3. Télécharger un modèle LLM :"
echo "     ollama pull llama3.2"
echo ""
echo "  4. Lancer le serveur Ari :"
echo "     ./scripts/run.sh"
echo ""
echo "  → Documentation API : http://localhost:8000/docs"
echo ""
log_success "Bon développement ! 🚀"

exit 0
