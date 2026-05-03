#!/bin/bash
# Study Bot — Ingest selector

echo ""
echo "Study Bot — Ingest"
echo "─────────────────────────────"
echo ""

# Collect ingest scripts
scripts=()
while IFS= read -r -d '' f; do
  scripts+=("$(basename "$f")")
done < <(find ingest -maxdepth 1 -name "*.py" -print0 | sort -z)

if [ ${#scripts[@]} -eq 0 ]; then
  echo "No ingest scripts found in ingest/"
  exit 1
fi

echo "Available ingest scripts:"
for i in "${!scripts[@]}"; do
  echo "  $((i+1))) ${scripts[$i]}"
done

echo ""
read -p "Which script would you like to run? [1-${#scripts[@]}]: " choice

if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt ${#scripts[@]} ]; then
  echo "Invalid selection."
  exit 1
fi

selected="${scripts[$((choice-1))]}"
echo ""
echo "Selected: $selected"
echo ""

source .venv/bin/activate

case "$selected" in

  ingest_textbook.py)
    read -p "PDF filename (in data/textbooks/): " filename
    read -p "Collection name (e.g. calculus): " collection
    read -p "Textbook title: " title
    read -p "Author name: " author
    read -p "Start page [1]: " start_page
    start_page="${start_page:-1}"

    PDF_PATH="data/textbooks/$filename"
    if [ ! -f "$PDF_PATH" ]; then
      echo "File not found: $PDF_PATH"
      exit 1
    fi

    echo ""
    echo "Starting ingest..."
    echo "  PDF:        $PDF_PATH"
    echo "  Collection: $collection"
    echo "  Title:      $title"
    echo "  Author:     $author"
    echo "  Start page: $start_page"
    echo ""

    caffeinate -d python ingest/$selected \
      --pdf "$PDF_PATH" \
      --collection "$collection" \
      --title "$title" \
      --author "$author" \
      --start-page "$start_page"
    ;;

  ingest_textbook_styled.py)
    read -p "PDF filename (in data/textbooks/): " filename
    read -p "Collection name (e.g. calculus): " collection
    read -p "Textbook title: " title
    read -p "Author name: " author
    read -p "Start page [1]: " start_page
    start_page="${start_page:-1}"

    PDF_PATH="data/textbooks/$filename"
    if [ ! -f "$PDF_PATH" ]; then
      echo "File not found: $PDF_PATH"
      exit 1
    fi

    echo ""
    echo "Starting styled ingest..."
    echo "  PDF:        $PDF_PATH"
    echo "  Collection: $collection"
    echo "  Title:      $title"
    echo "  Author:     $author"
    echo "  Start page: $start_page"
    echo ""

    caffeinate -d python ingest/$selected \
      --pdf "$PDF_PATH" \
      --collection "$collection" \
      --title "$title" \
      --author "$author" \
      --start-page "$start_page"
    ;;

  unity_docs_ingest.py)
    echo "Starting Unity docs ingest (no arguments required)..."
    echo ""
    caffeinate -d python ingest/$selected
    ;;

  *)
    echo "No handler defined for $selected — running with no arguments."
    caffeinate -d python ingest/$selected
    ;;

esac
