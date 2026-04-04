import os
import json
import re
import tempfile
import threading
import io
import difflib

from flask import Flask, jsonify, send_from_directory, make_response, request
from google import genai
from google.genai import types
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes,
)
import gspread
from google.oauth2.service_account import Credentials
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import cm

# ── Config ────────────────────────────────────────────────────────────────────
client_gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
flask_app = Flask(__name__, static_folder=".")

ATTENTE_CORRECTION = 1
COLONNES = ["Thème", "Source", "Référence", "Donnée", "Explication", "Traduction FR", "Lien"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
THEMES_PRINCIPAUX = [
    "Philosophie", "Religion", "Psychologie", "Économie", "Finance",
    "Histoire", "Sciences", "Développement personnel", "Politique"
]
notes_en_attente = {}

# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_sheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(os.environ["SPREADSHEET_ID"]).sheet1

def init_sheet(sheet):
    if not sheet.row_values(1):
        sheet.append_row(COLONNES)

def save_to_sheet(note: dict):
    sheet = get_sheet()
    init_sheet(sheet)
    sheet.append_row([
        note.get("theme", ""), note.get("source", ""), note.get("reference", ""),
        note.get("donnee", ""), note.get("explication", ""),
        note.get("traduction_fr", ""), note.get("lien", ""),
    ])

def get_all_notes() -> list:
    return get_sheet().get_all_records()

def update_row(row_index: int, note: dict):
    get_sheet().update(f"A{row_index}:G{row_index}", [[
        note.get("theme", ""), note.get("source", ""), note.get("reference", ""),
        note.get("donnee", ""), note.get("explication", ""),
        note.get("traduction_fr", ""), note.get("lien", ""),
    ]])

def delete_row(row_index: int):
    get_sheet().delete_rows(row_index)

# ── Flask API ─────────────────────────────────────────────────────────────────

@flask_app.route("/")
def index():
    return send_from_directory(".", "index.html")

@flask_app.route("/api/notes")
def api_notes():
    try:
        notes = get_all_notes()
        return jsonify({"success": True, "notes": notes})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@flask_app.route("/export-pdf")
def export_pdf_web():
    try:
        notes = get_all_notes()
        theme = request.args.get("theme", "").strip()
        source = request.args.get("source", "").strip()
        q = request.args.get("q", "").strip().lower()
        if theme:
            notes = [n for n in notes if (n.get("Thème") or "Autre") == theme]
        if source:
            notes = [n for n in notes if (n.get("Source") or "").split(":")[0].strip() == source]
        if q:
            notes = [n for n in notes if any(q in str(v).lower() for v in n.values())]
        buf = io.BytesIO()
        styles = getSampleStyleSheet()
        cell_style = ParagraphStyle("c", parent=styles["Normal"], fontSize=7, leading=10)
        head_style = ParagraphStyle("h", parent=styles["Normal"], fontSize=7, leading=10,
                                     textColor=colors.white, fontName="Helvetica-Bold")
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                leftMargin=1.5*cm, rightMargin=1.5*cm,
                                topMargin=2*cm, bottomMargin=2*cm)
        story = [Paragraph("Mes Notes", styles["Title"]), Spacer(1, 0.5*cm)]
        entete = [Paragraph(c, head_style) for c in COLONNES]
        donnees = [entete] + [
            [Paragraph(str(n.get(c) or ""), cell_style) for c in COLONNES]
            for n in notes
        ]
        largeurs = [2.5*cm, 3.5*cm, 2*cm, 4*cm, 4*cm, 2.5*cm, 1.5*cm]
        table = Table(donnees, colWidths=largeurs, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#3C3489")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#F4F4F8")]),
            ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("LEFTPADDING", (0,0), (-1,-1), 4),
        ]))
        story.append(table)
        doc.build(story)
        buf.seek(0)
        response = make_response(buf.read())
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = "inline; filename=mes_notes.pdf"
        return response
    except Exception as e:
        return f"Erreur : {str(e)}", 500

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, debug=False)

# ── Outils texte ──────────────────────────────────────────────────────────────

def cap(s: str) -> str:
    """Capitalise la première lettre d'une chaîne."""
    if not s:
        return s
    return s[0].upper() + s[1:]

def normaliser_source(source: str, notes: list) -> str:
    """Retourne la source existante la plus proche si similarité >= 0.65."""
    if not source or not notes:
        return source
    sources_existantes = list({n.get("Source", "") for n in notes if n.get("Source")})
    if not sources_existantes:
        return source
    meilleur_score = 0.0
    meilleure_source = source
    source_lower = source.lower()
    for s in sources_existantes:
        score = difflib.SequenceMatcher(None, source_lower, s.lower()).ratio()
        if score > meilleur_score:
            meilleur_score = score
            meilleure_source = s
    return meilleure_source if meilleur_score >= 0.65 else source

def contient_arabe(texte: str) -> bool:
    return bool(re.search(r'[\u0600-\u06FF]', texte))

def extraire_lien(texte: str):
    liens = re.findall(r'https?://\S+', texte)
    lien = liens[0] if liens else ""
    propre = re.sub(r'https?://\S+', '', texte).strip()
    return propre, lien

def capitaliser(s: str) -> str:
    """Met la première lettre en majuscule, laisse le reste intact."""
    if not s:
        return s
    return s[0].upper() + s[1:]

def trouver_source_similaire(source_candidate: str, seuil: float = 0.65) -> str | None:
    """Cherche une source existante similaire via fuzzy matching.

    Combine ratio séquentiel (difflib) et chevauchement de mots-clés pour
    gérer les variantes comme 'le livre capitalisme' → 'Livre : Le Capital de Karl Marx'.
    Retourne la source existante si le score dépasse le seuil, sinon None.
    """
    if not source_candidate:
        return None
    notes = get_all_notes()
    sources_existantes = list({
        n.get("Source", "").strip()
        for n in notes
        if n.get("Source", "").strip()
    })
    if not sources_existantes:
        return None

    cand = source_candidate.lower().strip()
    cand_type = cand.split(":")[0].strip() if ":" in cand else ""
    cand_mots = set(re.findall(r'\w{4,}', cand))

    meilleur_score = 0.0
    meilleure_source = None

    for src in sources_existantes:
        src_norm = src.lower().strip()
        score = difflib.SequenceMatcher(None, cand, src_norm).ratio()

        src_type = src_norm.split(":")[0].strip() if ":" in src_norm else ""
        src_mots = set(re.findall(r'\w{4,}', src_norm))

        # Chevauchement : "capitalisme" contient "capital" → match partiel
        overlap = any(cm in sm or sm in cm for cm in cand_mots for sm in src_mots)

        if cand_type and src_type and cand_type == src_type and overlap:
            score = max(score, 0.72)  # même type + mot-clé commun = signal fort
        elif overlap:
            score = max(score, 0.55)

        if score > meilleur_score:
            meilleur_score = score
            meilleure_source = src

    return meilleure_source if meilleur_score >= seuil else None

# ── Gemini ────────────────────────────────────────────────────────────────────

async def transcrire(file_path: str) -> str:
    with open(file_path, "rb") as f:
        audio_data = f.read()
    response = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(data=audio_data, mime_type="audio/ogg"),
            "Transcris exactement ce message vocal en gardant la langue originale "
            "(français, arabe, dialecte tunisien, anglais…). "
            "Réponds uniquement avec la transcription, sans aucun texte autour."
        ]
    )
    return response.text.strip()

async def detecter_intention(texte: str) -> dict:
    prompt = (
        "Analyse ce texte et détermine l'intention. Réponds UNIQUEMENT en JSON valide :\n\n"
        '{"type":"note|recherche|afficher|modifier|modifier_champ|supprimer|doute",'
        '"mot_cle":"sujet mentionné ou null","champ":"theme|source|reference|donnee|explication|lien ou null",'
        '"nouvelle_valeur":"nouvelle valeur si précisée ou null"}\n\n'
        "- 'note': information à sauvegarder\n"
        "- 'recherche': cherche/trouve/search/ابحث\n"
        "- 'afficher': montre/affiche/dernières notes\n"
        "- 'modifier': modifie/change/corrige/عدل\n"
        "- 'modifier_champ': modifie un champ précis avec nouvelle valeur\n"
        "- 'supprimer': supprime/efface/احذف\n"
        "- 'doute': impossible de déterminer\n\n"
        f"Texte : {texte}"
    )
    response = client_gemini.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    match = re.search(r"\{.*\}", response.text.strip(), re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"type": "note", "mot_cle": None, "champ": None, "nouvelle_valeur": None}

async def structurer(texte: str) -> dict:
    themes_str = ", ".join(THEMES_PRINCIPAUX)
    prompt = (
        "Tu es un assistant de prise de notes. Retourne UNIQUEMENT un JSON valide :\n\n"
        "{\n"
        f'  "theme": "thème parmi : {themes_str}. Sinon crée un thème pertinent en français",\n'
        '  "source": "combine type ET nom : \'Livre : Atomic Habits\', \'Formation : XYZ\', \'Podcast : Huberman\', \'Film : Inception\', \'Documentaire : XYZ\', \'Verset : Sourate Al-Baqara\', \'Réflexion personnelle\'. null si absent.",\n'
        '  "reference": "page, timestamp, sourate/verset, chapitre… null si absent",\n'
        '  "donnee": "concept principal dans la langue du texte",\n'
        '  "explication": "définition/contexte dans la langue du texte, null si absent",\n'
        '  "est_arabe": true ou false\n'
        "}\n\n"
        "- Dicte libre → déduis intelligemment chaque info.\n"
        "- 'dans le livre X' → 'Livre : X'\n"
        "- Ne pas dupliquer donnee et explication.\n"
        f"Texte : {texte}"
    )
    response = client_gemini.models.generate_content(model="gemini-2.5-flash", contents=prompt)
    match = re.search(r"\{.*\}", response.text.strip(), re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"theme": "Autre", "source": None, "reference": None,
            "donnee": texte, "explication": None, "est_arabe": False}

async def traduire_fr(texte: str) -> str:
    if not texte:
        return ""
    response = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"Traduis ce texte en français. Réponds uniquement avec la traduction :\n\n{texte}"
    )
    return response.text.strip()

# ── Formatage Telegram ────────────────────────────────────────────────────────

def formater(note: dict) -> str:
    champs = [
        ("🎯", "theme", "Thème"), ("📚", "source", "Source"), ("📍", "reference", "Référence"),
        ("💡", "donnee", "Donnée"), ("💬", "explication", "Explication"),
        ("🇫🇷", "traduction_fr", "Traduction FR"), ("🔗", "lien", "Lien"),
    ]
    lignes = []
    for emoji, cle, label in champs:
        val = note.get(cle) or note.get(label)
        if val:
            lignes.append(f"{emoji} *{label} :* {val}")
    return "\n".join(lignes)

# ── Traitement ────────────────────────────────────────────────────────────────

async def traiter_note(update: Update, texte_brut: str):
    texte, lien = extraire_lien(texte_brut)
    msg = await update.message.reply_text("✨ Analyse en cours…")
    data = await structurer(texte)
    traduction = ""
    if data.get("est_arabe") or contient_arabe(texte):
        await msg.edit_text("🔄 Traduction en cours…")
        parties = [p for p in [data.get("donnee"), data.get("explication")] if p]
        traduction = await traduire_fr(" — ".join(parties))

    note = {
        "theme": cap(data.get("theme") or "Autre"),
        "source": cap(data.get("source") or ""),
        "reference": str(data.get("reference")) if data.get("reference") else "",
        "donnee": cap(data.get("donnee") or texte),
        "explication": cap(data.get("explication") or ""),
        "traduction_fr": traduction,
        "lien": lien,
    }
    if note["source"]:
        note["source"] = normaliser_source(note["source"], get_all_notes())
    if not note["source"]:
        note_id = str(update.message.message_id)
        notes_en_attente[note_id] = note
        await msg.edit_text(
            f"ℹ️ Aucune source détectée.\n\n{formater(note)}\n\nClasser sans source ?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Sauvegarder", callback_data=f"save|{note_id}"),
                InlineKeyboardButton("❌ Annuler", callback_data=f"cancel|{note_id}"),
            ]])
        )
        return

    save_to_sheet(note)
    extra = (
        f"\n\n💡 Source reconnue : _{source_matchee}_"
        if source_matchee and source_matchee.lower() != source_originale.lower()
        else ""
    )
    await msg.edit_text(f"✅ *Sauvegardée !*\n\n{formater(note)}{extra}", parse_mode="Markdown")

async def traiter_commande(update, context, intention, texte):
    t = intention.get("type")
    mot = intention.get("mot_cle") or ""
    champ = intention.get("champ")
    valeur = intention.get("nouvelle_valeur")

    if t == "afficher":
        notes = get_all_notes()
        if not notes:
            await update.message.reply_text("Aucune note pour l'instant.")
            return
        await update.message.reply_text("📋 Tes 5 dernières notes :")
        for note in notes[-5:][::-1]:
            await update.message.reply_text(formater(note), parse_mode="Markdown")

    elif t == "recherche":
        if not mot:
            await update.message.reply_text("Je n'ai pas compris le mot à chercher.")
            return
        resultats = [(i+2, n) for i, n in enumerate(get_all_notes()) if mot.lower() in str(n).lower()]
        if not resultats:
            await update.message.reply_text(f"🔍 Aucun résultat pour « {mot} ».")
            return
        await update.message.reply_text(f"🔍 {len(resultats)} résultat(s) pour « {mot} » :")
        for _, note in resultats[-5:]:
            await update.message.reply_text(formater(note), parse_mode="Markdown")

    elif t == "modifier_champ" and mot and champ and valeur:
        resultats = [(i+2, n) for i, n in enumerate(get_all_notes()) if mot.lower() in str(n).lower()]
        if not resultats:
            await update.message.reply_text(f"Aucune note trouvée pour « {mot} ».")
            return
        row, note = resultats[0]
        note_modif = {
            "theme": note.get("Thème", ""), "source": note.get("Source", ""),
            "reference": note.get("Référence", ""), "donnee": note.get("Donnée", ""),
            "explication": note.get("Explication", ""), "traduction_fr": note.get("Traduction FR", ""),
            "lien": note.get("Lien", ""),
        }
        note_modif[champ] = valeur
        update_row(row, note_modif)
        await update.message.reply_text(f"✅ *Mis à jour !*\n\n{formater(note_modif)}", parse_mode="Markdown")

    elif t == "modifier":
        if not mot:
            await update.message.reply_text("Précise quelle note modifier.")
            return
        resultats = [(i+2, n) for i, n in enumerate(get_all_notes()) if mot.lower() in str(n).lower()]
        if not resultats:
            await update.message.reply_text(f"Aucune note trouvée pour « {mot} ».")
            return
        row, note = resultats[0]
        context.user_data["row"] = row
        context.user_data["note"] = note
        await update.message.reply_text(
            f"📝 Note trouvée :\n\n{formater(note)}\n\nDicte la version corrigée :",
            parse_mode="Markdown",
        )
        return ATTENTE_CORRECTION

    elif t == "supprimer":
        if not mot:
            await update.message.reply_text("Précise quelle note supprimer.")
            return
        resultats = [(i+2, n) for i, n in enumerate(get_all_notes()) if mot.lower() in str(n).lower()]
        if not resultats:
            await update.message.reply_text(f"Aucune note trouvée pour « {mot} ».")
            return
        row, note = resultats[0]
        note_id = f"del_{row}"
        notes_en_attente[note_id] = {"row": row}
        await update.message.reply_text(
            f"🗑️ Supprimer cette note ?\n\n{formater(note)}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Oui", callback_data=f"delete|{note_id}"),
                InlineKeyboardButton("❌ Non", callback_data=f"cancel|{note_id}"),
            ]])
        )

    elif t == "doute":
        note_id = str(update.message.message_id)
        notes_en_attente[note_id] = {"texte": texte}
        await update.message.reply_text(
            "🤔 C'est une nouvelle note ou une commande ?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📝 Nouvelle note", callback_data=f"note|{note_id}"),
                InlineKeyboardButton("⚙️ Commande", callback_data=f"cmd|{note_id}"),
            ]])
        )

async def handler_principal(update, context, texte):
    intention = await detecter_intention(texte)
    if intention.get("type") == "note":
        await traiter_note(update, texte)
    else:
        await traiter_commande(update, context, intention, texte)

# ── Commandes Telegram ────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 *Bot de Notes*\n\n"
        "Envoie un *vocal* ou *texte* — je détecte si c'est une note ou une commande\\.\n\n"
        "*Commandes vocales :*\n"
        "• _\"Cherche mes notes sur la philosophie\"_\n"
        "• _\"Montre mes dernières notes\"_\n"
        "• _\"Modifie la note sur les habitudes\"_\n"
        "• _\"Supprime la note sur le stoïcisme\"_\n\n"
        "*Commandes texte :*\n"
        "/notes — 5 dernières notes\n"
        "/chercher \\[mot\\] — rechercher\n"
        "/modifier \\[mot\\] — modifier\n"
        "/supprimer \\[mot\\] — supprimer\n"
        "/export — PDF via Telegram",
        parse_mode="MarkdownV2",
    )

async def cmd_vocal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🎙️ Transcription en cours…")
    voice = update.message.voice
    fich = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await fich.download_to_drive(tmp.name)
        path = tmp.name
    try:
        texte = await transcrire(path)
    finally:
        os.unlink(path)
    await msg.edit_text(f"🗣️ _{texte}_", parse_mode="Markdown")
    await handler_principal(update, context, texte)

async def cmd_texte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handler_principal(update, context, update.message.text)

async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notes = get_all_notes()
    if not notes:
        await update.message.reply_text("Aucune note pour l'instant.")
        return
    for note in notes[-5:][::-1]:
        await update.message.reply_text(formater(note), parse_mode="Markdown")

async def cmd_chercher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /chercher mot-clé")
        return
    q = " ".join(context.args).lower()
    resultats = [(i+2, n) for i, n in enumerate(get_all_notes()) if q in str(n).lower()]
    if not resultats:
        await update.message.reply_text(f"Aucun résultat pour « {q} ».")
        return
    await update.message.reply_text(f"🔍 {len(resultats)} résultat(s) :")
    for _, note in resultats[-5:]:
        await update.message.reply_text(formater(note), parse_mode="Markdown")

async def cmd_modifier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /modifier mot-clé")
        return
    q = " ".join(context.args).lower()
    resultats = [(i+2, n) for i, n in enumerate(get_all_notes()) if q in str(n).lower()]
    if not resultats:
        await update.message.reply_text(f"Aucun résultat pour « {q} ».")
        return
    row, note = resultats[0]
    context.user_data["row"] = row
    context.user_data["note"] = note
    await update.message.reply_text(
        f"📝 Note trouvée :\n\n{formater(note)}\n\nDicte la version corrigée :",
        parse_mode="Markdown",
    )
    return ATTENTE_CORRECTION

async def recevoir_correction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texte, lien = extraire_lien(update.message.text)
    row = context.user_data.get("row")
    ancien_lien = context.user_data.get("note", {}).get("Lien", "")
    msg = await update.message.reply_text("✨ Restructuration en cours…")
    data = await structurer(texte)
    traduction = ""
    if data.get("est_arabe") or contient_arabe(texte):
        parties = [p for p in [data.get("donnee"), data.get("explication")] if p]
        traduction = await traduire_fr(" — ".join(parties))

    note = {
        "theme": cap(data.get("theme") or "Autre"),
        "source": cap(data.get("source") or ""),
        "reference": str(data.get("reference")) if data.get("reference") else "",
        "donnee": cap(data.get("donnee") or texte),
        "explication": cap(data.get("explication") or ""),
        "traduction_fr": traduction,
        "lien": lien or ancien_lien,
    }
    if note["source"]:
        note["source"] = normaliser_source(note["source"], get_all_notes())
    update_row(row, note)
    extra = (
        f"\n\n💡 Source reconnue : _{source_matchee}_"
        if source_matchee and source_matchee.lower() != source_originale.lower()
        else ""
    )
    await msg.edit_text(f"✅ *Note modifiée !*\n\n{formater(note)}{extra}", parse_mode="Markdown")
    return ConversationHandler.END

async def cmd_supprimer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /supprimer mot-clé")
        return
    q = " ".join(context.args).lower()
    resultats = [(i+2, n) for i, n in enumerate(get_all_notes()) if q in str(n).lower()]
    if not resultats:
        await update.message.reply_text(f"Aucun résultat pour « {q} ».")
        return
    row, note = resultats[0]
    note_id = f"del_{row}"
    notes_en_attente[note_id] = {"row": row}
    await update.message.reply_text(
        f"🗑️ Supprimer cette note ?\n\n{formater(note)}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Oui", callback_data=f"delete|{note_id}"),
            InlineKeyboardButton("❌ Non", callback_data=f"cancel|{note_id}"),
        ]])
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Modification annulée.")
    return ConversationHandler.END

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notes = get_all_notes()
    if not notes:
        await update.message.reply_text("Aucune note à exporter.")
        return
    msg = await update.message.reply_text("📄 Génération du PDF…")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        path = tmp.name
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("c", parent=styles["Normal"], fontSize=7, leading=10)
    head_style = ParagraphStyle("h", parent=styles["Normal"], fontSize=7, leading=10,
                                 textColor=colors.white, fontName="Helvetica-Bold")
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=1.5*cm, rightMargin=1.5*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    story = [Paragraph("Mes Notes", styles["Title"]), Spacer(1, 0.5*cm)]
    entete = [Paragraph(c, head_style) for c in COLONNES]
    donnees = [entete] + [[Paragraph(str(n.get(c) or ""), cell_style) for c in COLONNES] for n in notes]
    largeurs = [2.5*cm, 3.5*cm, 2*cm, 4*cm, 4*cm, 2.5*cm, 1.5*cm]
    table = Table(donnees, colWidths=largeurs, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#3C3489")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#F4F4F8")]),
        ("GRID", (0,0), (-1,-1), 0.3, colors.HexColor("#CCCCCC")),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("TOPPADDING", (0,0), (-1,-1), 4), ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LEFTPADDING", (0,0), (-1,-1), 4),
    ]))
    story.append(table)
    doc.build(story)
    await msg.edit_text(f"✅ {len(notes)} note(s) exportées !")
    try:
        with open(path, "rb") as f:
            await update.message.reply_document(f, filename="mes_notes.pdf")
    finally:
        os.unlink(path)

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data.startswith("save|"):
        note_id = data[5:]
        note = notes_en_attente.pop(note_id, None)
        if note:
            save_to_sheet(note)
            await q.edit_message_text(f"✅ *Sauvegardée !*\n\n{formater(note)}", parse_mode="Markdown")
        else:
            await q.edit_message_text("❌ Note expirée, renvoie-la.")
    elif data.startswith("note|"):
        note_id = data[5:]
        info = notes_en_attente.pop(note_id, None)
        if info and "texte" in info:
            await traiter_note(update, info["texte"])
    elif data.startswith("cmd|"):
        note_id = data[4:]
        info = notes_en_attente.pop(note_id, None)
        if info and "texte" in info:
            intention = await detecter_intention(info["texte"])
            await traiter_commande(update, context, intention, info["texte"])
    elif data.startswith("delete|"):
        note_id = data[7:]
        info = notes_en_attente.pop(note_id, None)
        if info:
            delete_row(info["row"])
            await q.edit_message_text("🗑️ Note supprimée.")
        else:
            await q.edit_message_text("❌ Note introuvable.")
    elif data.startswith("cancel|"):
        notes_en_attente.pop(data[7:], None)
        await q.edit_message_text("❌ Annulé.")

# ── Lancement ─────────────────────────────────────────────────────────────────

def main():
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("modifier", cmd_modifier)],
        states={ATTENTE_CORRECTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, recevoir_correction)]},
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("notes", cmd_notes))
    app.add_handler(CommandHandler("chercher", cmd_chercher))
    app.add_handler(CommandHandler("supprimer", cmd_supprimer))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.VOICE, cmd_vocal))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_texte))
    print("✅ Bot + Interface web démarrés.")
    app.run_polling()

if __name__ == "__main__":
    main()
