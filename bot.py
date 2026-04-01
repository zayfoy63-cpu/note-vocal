import os
import json
import re
import tempfile

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

# ── Config Gemini ─────────────────────────────────────────────────────────────
client_gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# ── États ConversationHandler ────────────────────────────────────────────────
ATTENTE_CORRECTION = 1

# ── Colonnes Google Sheets ───────────────────────────────────────────────────
COLONNES = ["Thème", "Source", "Référence", "Donnée", "Explication", "Traduction FR", "Lien"]
SCOPES   = ["https://www.googleapis.com/auth/spreadsheets"]

# Thèmes principaux — le bot peut en créer d'autres si nécessaire
THEMES_PRINCIPAUX = [
    "Philosophie", "Religion", "Psychologie", "Économie", "Finance",
    "Histoire", "Sciences", "Développement personnel", "Politique"
]

# ── Stockage temporaire notes en attente ─────────────────────────────────────
notes_en_attente = {}

# ── Google Sheets ────────────────────────────────────────────────────────────

def get_sheet():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds      = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client     = gspread.authorize(creds)
    return client.open_by_key(os.environ["SPREADSHEET_ID"]).sheet1

def init_sheet(sheet):
    if not sheet.row_values(1):
        sheet.append_row(COLONNES)

def save_to_sheet(note: dict):
    sheet = get_sheet()
    init_sheet(sheet)
    sheet.append_row([
        note.get("theme",         ""),
        note.get("source",        ""),
        note.get("reference",     ""),
        note.get("donnee",        ""),
        note.get("explication",   ""),
        note.get("traduction_fr", ""),
        note.get("lien",          ""),
    ])

def get_all_notes() -> list:
    return get_sheet().get_all_records()

def update_row(row_index: int, note: dict):
    get_sheet().update(f"A{row_index}:G{row_index}", [[
        note.get("theme",         ""),
        note.get("source",        ""),
        note.get("reference",     ""),
        note.get("donnee",        ""),
        note.get("explication",   ""),
        note.get("traduction_fr", ""),
        note.get("lien",          ""),
    ]])

def delete_row(row_index: int):
    get_sheet().delete_rows(row_index)

# ── Outils texte ─────────────────────────────────────────────────────────────

def contient_arabe(texte: str) -> bool:
    return bool(re.search(r'[\u0600-\u06FF]', texte))

def extraire_lien(texte: str):
    liens  = re.findall(r'https?://\S+', texte)
    lien   = liens[0] if liens else ""
    propre = re.sub(r'https?://\S+', '', texte).strip()
    return propre, lien

# ── Gemini : transcription vocale ─────────────────────────────────────────────

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

# ── Gemini : structuration ────────────────────────────────────────────────────

async def structurer(texte: str) -> dict:
    themes_str = ", ".join(THEMES_PRINCIPAUX)
    prompt = (
        "Tu es un assistant de prise de notes intelligent. "
        "Analyse ce texte et retourne UNIQUEMENT un objet JSON valide (sans markdown, sans texte autour) :\n\n"
        "{\n"
        f'  "theme": "le thème principal parmi : {themes_str}. Si aucun ne correspond exactement, crée un thème pertinent en français",\n'
        '  "source": "combine le type ET le nom : ex \'Livre : Atomic Habits\', \'Formation : XYZ\', \'Podcast : Huberman\', \'Film : Inception\', \'Documentaire : XYZ\', \'Verset : Sourate Al-Baqara\', \'Réflexion personnelle\'. null si aucune source.",\n'
        '  "reference": "page, timestamp (ex: 12:34), numéro de sourate/verset, chapitre, épisode… null si absent",\n'
        '  "donnee": "le mot, la date, la citation, le concept principal — dans la langue du texte",\n'
        '  "explication": "définition, signification, contexte de la donnée — dans la langue du texte, null si absent",\n'
        '  "est_arabe": true ou false\n'
        "}\n\n"
        "Règles importantes :\n"
        "- La personne dicte librement dans n'importe quel ordre → déduis intelligemment où chaque info va.\n"
        "- Pour 'source' : si la personne dit 'dans le livre X' → 'Livre : X', 'dans la formation X' → 'Formation : X', etc.\n"
        "- Si aucune source identifiable → source=null, reference=null.\n"
        "- Ne jamais dupliquer la même info dans donnee et explication.\n"
        "- est_arabe = true si le texte contient de l'arabe ou du dialecte arabe.\n\n"
        f"Texte : {texte}"
    )
    response = client_gemini.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    contenu = response.text.strip()
    match   = re.search(r"\{.*\}", contenu, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"theme": "Autre", "source": None, "reference": None,
            "donnee": texte, "explication": None, "est_arabe": False}

# ── Gemini : traduction ───────────────────────────────────────────────────────

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
        ("🎯", "theme",         "Thème"),
        ("📚", "source",        "Source"),
        ("📍", "reference",     "Référence"),
        ("💡", "donnee",        "Donnée"),
        ("💬", "explication",   "Explication"),
        ("🇫🇷", "traduction_fr", "Traduction FR"),
        ("🔗", "lien",          "Lien"),
    ]
    lignes = []
    for emoji, cle, label in champs:
        val = note.get(cle) or note.get(label)
        if val:
            lignes.append(f"{emoji} *{label} :* {val}")
    return "\n".join(lignes)

# ── Traitement d'un message ───────────────────────────────────────────────────

async def traiter(update: Update, texte_brut: str):
    texte, lien = extraire_lien(texte_brut)
    msg = await update.message.reply_text("✨ Analyse en cours…")

    data = await structurer(texte)

    traduction = ""
    if data.get("est_arabe") or contient_arabe(texte):
        await msg.edit_text("🔄 Traduction en cours…")
        parties    = [p for p in [data.get("donnee"), data.get("explication")] if p]
        traduction = await traduire_fr(" — ".join(parties))

    note = {
        "theme":         data.get("theme")       or "Autre",
        "source":        data.get("source")      or "",
        "reference":     str(data.get("reference")) if data.get("reference") else "",
        "donnee":        data.get("donnee")      or texte,
        "explication":   data.get("explication") or "",
        "traduction_fr": traduction,
        "lien":          lien,
    }

    # Note sans source → confirmation
    if not note["source"]:
        note_id = str(update.message.message_id)
        notes_en_attente[note_id] = note
        await msg.edit_text(
            f"ℹ️ Aucune source détectée.\n\n{formater(note)}\n\n"
            "Je classe sans source — c'est correct ?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Sauvegarder", callback_data=f"save|{note_id}"),
                InlineKeyboardButton("❌ Annuler",     callback_data=f"cancel|{note_id}"),
            ]])
        )
        return

    save_to_sheet(note)
    await msg.edit_text(f"✅ *Sauvegardée !*\n\n{formater(note)}", parse_mode="Markdown")

# ── Commandes ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 *Bot de Notes*\n\n"
        "Envoie un *vocal* ou un *texte* — je structure et sauvegarde dans Google Sheets\\.\n\n"
        "Exemples :\n"
        "• _\"Dans le livre Atomic Habits page 47, la règle des 1%…\"_\n"
        "• _\"Formation marketing, module 3, le tunnel de vente c'est…\"_\n"
        "• _\"Podcast Huberman à 12:34, le sommeil et la mémoire…\"_\n"
        "• _\"Verset Sourate Al\\-Baqara 286, لا يكلف الله…\"_ → traduction FR auto\n"
        "• _\"J'ai une réflexion sur la discipline…\"_ → sans source\n\n"
        "*Commandes :*\n"
        "/notes — 5 dernières notes\n"
        "/chercher \\[mot\\] — rechercher\n"
        "/modifier \\[mot\\] — modifier une note\n"
        "/supprimer \\[mot\\] — supprimer une note\n"
        "/export — toutes les notes en PDF",
        parse_mode="MarkdownV2",
    )

async def cmd_vocal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg   = await update.message.reply_text("🎙️ Transcription en cours…")
    voice = update.message.voice
    fich  = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await fich.download_to_drive(tmp.name)
        path = tmp.name

    try:
        texte = await transcrire(path)
    finally:
        os.unlink(path)

    await msg.edit_text(f"🗣️ _{texte}_", parse_mode="Markdown")
    await traiter(update, texte)

async def cmd_texte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await traiter(update, update.message.text)

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
    q         = " ".join(context.args).lower()
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
    q         = " ".join(context.args).lower()
    resultats = [(i+2, n) for i, n in enumerate(get_all_notes()) if q in str(n).lower()]
    if not resultats:
        await update.message.reply_text(f"Aucun résultat pour « {q} ».")
        return ConversationHandler.END

    row, note = resultats[0]
    context.user_data["row"]  = row
    context.user_data["note"] = note
    await update.message.reply_text(
        f"📝 Note trouvée :\n\n{formater(note)}\n\nDicte ou tape la version corrigée :",
        parse_mode="Markdown",
    )
    return ATTENTE_CORRECTION

async def recevoir_correction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texte, lien = extraire_lien(update.message.text)
    row         = context.user_data.get("row")
    ancien_lien = context.user_data.get("note", {}).get("Lien", "")

    msg  = await update.message.reply_text("✨ Restructuration en cours…")
    data = await structurer(texte)

    traduction = ""
    if data.get("est_arabe") or contient_arabe(texte):
        parties    = [p for p in [data.get("donnee"), data.get("explication")] if p]
        traduction = await traduire_fr(" — ".join(parties))

    note = {
        "theme":         data.get("theme")       or "Autre",
        "source":        data.get("source")      or "",
        "reference":     str(data.get("reference")) if data.get("reference") else "",
        "donnee":        data.get("donnee")      or texte,
        "explication":   data.get("explication") or "",
        "traduction_fr": traduction,
        "lien":          lien or ancien_lien,
    }
    update_row(row, note)
    await msg.edit_text(f"✅ *Note modifiée !*\n\n{formater(note)}", parse_mode="Markdown")
    return ConversationHandler.END

async def cmd_supprimer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /supprimer mot-clé")
        return
    q         = " ".join(context.args).lower()
    resultats = [(i+2, n) for i, n in enumerate(get_all_notes()) if q in str(n).lower()]
    if not resultats:
        await update.message.reply_text(f"Aucun résultat pour « {q} ».")
        return

    row, note = resultats[0]
    note_id   = f"del_{row}"
    notes_en_attente[note_id] = {"row": row}
    await update.message.reply_text(
        f"🗑️ Supprimer cette note ?\n\n{formater(note)}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Oui", callback_data=f"delete|{note_id}"),
            InlineKeyboardButton("❌ Non", callback_data=f"cancel|{note_id}"),
        ]])
    )

async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notes = get_all_notes()
    if not notes:
        await update.message.reply_text("Aucune note à exporter.")
        return

    msg = await update.message.reply_text("📄 Génération du PDF…")

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        path = tmp.name

    styles     = getSampleStyleSheet()
    cell_style = ParagraphStyle("c", parent=styles["Normal"], fontSize=8, leading=11)
    head_style = ParagraphStyle("h", parent=styles["Normal"], fontSize=8, leading=11,
                                 textColor=colors.white, fontName="Helvetica-Bold")

    doc   = SimpleDocTemplate(path, pagesize=A4,
                              leftMargin=1.5*cm, rightMargin=1.5*cm,
                              topMargin=2*cm, bottomMargin=2*cm)
    story = [Paragraph("Mes Notes", styles["Title"]), Spacer(1, 0.5*cm)]

    entete  = [Paragraph(c, head_style) for c in COLONNES]
    donnees = [entete] + [
        [Paragraph(str(n.get(c) or ""), cell_style) for c in COLONNES]
        for n in notes
    ]

    largeurs = [2.5*cm, 3.5*cm, 2*cm, 4*cm, 4*cm, 2.5*cm, 1.5*cm]
    table    = Table(donnees, colWidths=largeurs, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0),  colors.HexColor("#3C3489")),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, colors.HexColor("#F4F4F8")]),
        ("GRID",          (0,0), (-1,-1), 0.3, colors.HexColor("#CCCCCC")),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LEFTPADDING",   (0,0), (-1,-1), 4),
    ]))
    story.append(table)
    doc.build(story)

    await msg.edit_text(f"✅ {len(notes)} note(s) exportées !")
    try:
        with open(path, "rb") as f:
            await update.message.reply_document(f, filename="mes_notes.pdf")
    finally:
        os.unlink(path)

# ── Callbacks boutons ─────────────────────────────────────────────────────────

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("save|"):
        note_id = data[5:]
        note    = notes_en_attente.pop(note_id, None)
        if note:
            save_to_sheet(note)
            await q.edit_message_text(f"✅ *Sauvegardée !*\n\n{formater(note)}", parse_mode="Markdown")
        else:
            await q.edit_message_text("❌ Note expirée, renvoie-la.")

    elif data.startswith("delete|"):
        note_id = data[7:]
        info    = notes_en_attente.pop(note_id, None)
        if info:
            delete_row(info["row"])
            await q.edit_message_text("🗑️ Note supprimée.")
        else:
            await q.edit_message_text("❌ Note introuvable.")

    elif data.startswith("cancel|"):
        note_id = data[7:]
        notes_en_attente.pop(note_id, None)
        await q.edit_message_text("❌ Annulé.")

# ── Lancement ─────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("modifier", cmd_modifier)],
        states={ATTENTE_CORRECTION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, recevoir_correction)
        ]},
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("notes",     cmd_notes))
    app.add_handler(CommandHandler("chercher",  cmd_chercher))
    app.add_handler(CommandHandler("supprimer", cmd_supprimer))
    app.add_handler(CommandHandler("export",    cmd_export))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.VOICE, cmd_vocal))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_texte))

    print("✅ Bot démarré.")
    app.run_polling()

if __name__ == "__main__":
    main()
