#!/usr/bin/env python3
"""
RagFlow Log Analyzer
Analysiert RagFlow Logfiles und extrahiert Konversationen mit Claude
"""

import re
import json
import sys
import argparse
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass
import os

@dataclass
class Message:
    timestamp: datetime
    user_message: str
    claude_response: str
    context_documents: List[Dict[str, str]]

@dataclass
class Conversation:
    chat_id: str
    first_message: str
    start_time: datetime
    message_count: int
    messages: List[Message]

class RagFlowLogAnalyzer:
    def __init__(self, logfile_path: str):
        self.logfile_path = logfile_path
        self.conversations: Dict[str, Conversation] = {}
        
    def parse_timestamp(self, timestamp_str: str) -> datetime:
        """Parst einen Timestamp aus dem Logfile"""
        try:
            return datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S,%f")
        except ValueError:
            try:
                return datetime.strptime(timestamp_str[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return datetime.now()
    
    def extract_context_documents(self, log_content: str) -> List[Dict[str, str]]:
        """Extrahiert Kontext-Dokumente aus einem Log-Eintrag"""
        documents = []

        # Decode escaped newlines wenn sie vorhanden sind
        if '\\n' in log_content:
            log_content = log_content.replace('\\n', '\n')

        # Suche nach dem Coverdale-Wissen Pattern
        pattern = r'ID:\s*(\d+)\s*├──\s*Title:\s*([^\n]+)\s*└──\s*Content:\s*(.*?)(?=\n\s*------|\nID:|\n\s*\}\s*,|\n\s*\]\s*\}|\Z)'
        matches = re.findall(pattern, log_content, re.DOTALL)
        
        for match in matches:
            doc_id, title, content = match
            content_clean = content.strip()
            content_clean = re.sub(r'\s*\}\s*,?\s*\]\s*\}?\s*$', '', content_clean)
            
            documents.append({
                'id': doc_id.strip(),
                'title': title.strip(),
                'content': content_clean
            })
        
        return documents

    def determine_conversation_id(self, base_chat_id: str, user_message: str, timestamp: datetime, history_data: List[Dict]) -> str:
        """Bestimmt eine eindeutige Konversations-ID basierend auf verschiedenen Heuristiken"""

        # Heuristik 1: Prüfe explizite Konversationsnummern
        pattern = r'konversation\s+(\d+)'
        match = re.search(pattern, user_message.lower())
        if match:
            conv_num = match.group(1)
            return f"{base_chat_id}_conv{conv_num}"

        # Heuristik 2: Prüfe HISTORY-Reset (nur System + User = neue Konversation)
        user_messages = [msg for msg in history_data if msg.get('role') == 'user']
        if len(user_messages) == 1:
            # Erste User-Message in dieser HISTORY = potentiell neue Konversation
            # Prüfe zusätzlich Zeitabstand
            if self._should_start_new_conversation_by_time(base_chat_id, timestamp):
                return f"{base_chat_id}_{int(timestamp.timestamp())}"

        # Heuristik 3: Prüfe Zeitabstand zur letzten Message derselben Chat-ID
        if self._should_start_new_conversation_by_time(base_chat_id, timestamp):
            return f"{base_chat_id}_{int(timestamp.timestamp())}"

        # Heuristik 4: Kurze Test-Messages nach Pause
        if self._is_short_test_message(user_message):
            if self._should_start_new_conversation_by_time(base_chat_id, timestamp, threshold_minutes=30):
                return f"{base_chat_id}_test_{int(timestamp.timestamp())}"

        # Fallback: Finde die aktuellste Konversation für diese Chat-ID
        return self._get_current_conversation_id(base_chat_id)

    def _should_start_new_conversation_by_time(self, base_chat_id: str, current_timestamp: datetime, threshold_minutes: int = 120) -> bool:
        """Prüft ob basierend auf Zeitabstand eine neue Konversation gestartet werden sollte"""
        if not self.conversations:
            return True

        # Finde die neueste Message für diese Chat-ID
        latest_timestamp = None
        for conv_id, conv in self.conversations.items():
            if conv_id.startswith(base_chat_id) and conv.messages:
                conv_latest = max(msg.timestamp for msg in conv.messages)
                if latest_timestamp is None or conv_latest > latest_timestamp:
                    latest_timestamp = conv_latest

        if latest_timestamp is None:
            return True

        # Prüfe Zeitabstand
        time_diff_minutes = (current_timestamp - latest_timestamp).total_seconds() / 60
        return time_diff_minutes > threshold_minutes

    def _is_short_test_message(self, user_message: str) -> bool:
        """Erkennt kurze Test-Messages"""
        msg = user_message.strip().lower()
        return len(msg) <= 10 and (msg.startswith('test') or msg in ['test', 'hallo', 'hi', 'hello'])

    def _get_current_conversation_id(self, base_chat_id: str) -> str:
        """Findet die aktuellste Konversations-ID für eine Chat-ID oder erstellt eine neue"""
        # Finde alle Konversationen für diese Chat-ID
        matching_convs = [(conv_id, conv) for conv_id, conv in self.conversations.items()
                         if conv_id.startswith(base_chat_id)]

        if not matching_convs:
            return base_chat_id

        # Finde die neueste Konversation
        latest_conv = max(matching_convs, key=lambda x: max(msg.timestamp for msg in x[1].messages))
        return latest_conv[0]

    def _find_claude_response_after_post(self, post_timestamp: datetime, user_message: str) -> str:
        """Findet die Claude-Antwort im nächsten HISTORY-Block nach dem POST-Request"""

        # Lade das Logfile neu und suche nach HISTORY-Blöcken nach dem POST-Timestamp
        with open(self.logfile_path, 'r', encoding='utf-8') as file:
            content = file.read()

        lines = content.split('\n')

        for i, line in enumerate(lines):
            if '[HISTORY][' in line:
                # Extrahiere Timestamp
                timestamp_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', line)
                if timestamp_match:
                    timestamp_str = timestamp_match.group(1)
                    history_timestamp = self.parse_timestamp(timestamp_str)

                    # Nur HISTORY-Blöcke nach dem POST-Request betrachten
                    if history_timestamp > post_timestamp:
                        # Sammle JSON-Inhalt des HISTORY-Blocks
                        json_lines = [line]
                        j = i + 1

                        while j < len(lines):
                            next_line = lines[j]
                            if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', next_line):
                                break
                            json_lines.append(next_line)
                            j += 1

                        # Parse JSON und suche nach Assistant-Antwort
                        try:
                            json_content = '\n'.join(json_lines)
                            # Finde JSON-Start nach [HISTORY][
                            history_marker = json_content.find('[HISTORY][')
                            if history_marker != -1:
                                json_start = history_marker + len('[HISTORY][')
                                json_part = json_content[json_start:]

                                # Robuste JSON-Extraktion wie in analyze_logfile
                                json_part = json_part.strip()
                                if not json_part.startswith('['):
                                    if json_part.startswith('{'):
                                        json_part = '[' + json_part
                                    else:
                                        continue

                                bracket_count = 0
                                in_string = False
                                escape_next = False
                                json_end = len(json_part)

                                for k, char in enumerate(json_part):
                                    if escape_next:
                                        escape_next = False
                                        continue
                                    if char == '\\' and in_string:
                                        escape_next = True
                                        continue
                                    if char == '"' and not escape_next:
                                        in_string = not in_string
                                    if not in_string:
                                        if char == '[':
                                            bracket_count += 1
                                        elif char == ']':
                                            bracket_count -= 1
                                            if bracket_count == 0:
                                                json_end = k + 1
                                                break

                                json_str = json_part[:json_end]
                                history_data = json.loads(json_str)

                                # Suche nach der User-Nachricht und der darauffolgenden Assistant-Antwort
                                for idx, msg in enumerate(history_data):
                                    if (msg.get('role') == 'user' and
                                        msg.get('content', '').strip() == user_message.strip()):
                                        # Finde die nächste Assistant-Nachricht
                                        for next_idx in range(idx + 1, len(history_data)):
                                            if history_data[next_idx].get('role') == 'assistant':
                                                assistant_content = history_data[next_idx].get('content', '').strip()
                                                if assistant_content:
                                                    return assistant_content

                        except (json.JSONDecodeError, ValueError):
                            continue

        return None

    def analyze_logfile(self) -> None:
        """Analysiert das komplette Logfile mit neuem Ansatz: vom Ende her arbeiten"""
        print("Analysiere Logfile...")

        with open(self.logfile_path, 'r', encoding='utf-8') as file:
            content = file.read()

        lines = content.split('\n')
        print(f"Logfile hat {len(lines)} Zeilen")

        # Sammle alle HISTORY-Blöcke
        history_blocks = []
        i = 0
        while i < len(lines):
            line = lines[i]

            if '[HISTORY][' in line:
                # Extrahiere Timestamp
                timestamp_match = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})', line)
                if timestamp_match:
                    timestamp_str = timestamp_match.group(1)
                    timestamp = self.parse_timestamp(timestamp_str)

                    # Sammle den kompletten Block
                    block_lines = [line]
                    j = i + 1

                    while j < len(lines):
                        next_line = lines[j]
                        if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', next_line):
                            break
                        block_lines.append(next_line)
                        j += 1

                    block_content = '\n'.join(block_lines)

                    history_blocks.append({
                        'timestamp': timestamp,
                        'timestamp_str': timestamp_str,
                        'content': block_content,
                        'line_number': i
                    })

                    i = j
                else:
                    i += 1
            else:
                i += 1

        print(f"Gefundene HISTORY-Blöcke: {len(history_blocks)}")

        # Sortiere HISTORY-Blöcke rückwärts chronologisch (neueste zuerst)
        history_blocks.sort(key=lambda x: x['timestamp'], reverse=True)

        # Sammle alle gefundenen Konversationen (als Set von User-Nachrichten-Sequenzen)
        found_conversations = []

        for hist_block in history_blocks:
            timestamp = hist_block['timestamp']
            timestamp_str = hist_block['timestamp_str']
            block_content = hist_block['content']

            # Parse JSON aus dem HISTORY-Block
            try:
                # Finde JSON-Start nach [HISTORY][ wie in der ursprünglichen Implementierung
                history_marker = block_content.find('[HISTORY][')
                if history_marker == -1:
                    continue

                json_start = history_marker + len('[HISTORY][')
                json_part = block_content[json_start:]

                # Robuste JSON-Extraktion (wie vorher)
                json_part = json_part.strip()
                if not json_part.startswith('['):
                    if json_part.startswith('{'):
                        json_part = '[' + json_part
                    else:
                        continue

                bracket_count = 0
                in_string = False
                escape_next = False
                json_end = len(json_part)

                for k, char in enumerate(json_part):
                    if escape_next:
                        escape_next = False
                        continue
                    if char == '\\' and in_string:
                        escape_next = True
                        continue
                    if char == '"' and not escape_next:
                        in_string = not in_string
                    if not in_string:
                        if char == '[':
                            bracket_count += 1
                        elif char == ']':
                            bracket_count -= 1
                            if bracket_count == 0:
                                json_end = k + 1
                                break

                json_str = json_part[:json_end]
                history_data = json.loads(json_str)

                # Extrahiere User-Nachrichten-Sequenz
                user_messages = [msg.get('content', '').strip() for msg in history_data if msg.get('role') == 'user']

                if not user_messages:
                    continue

                # Prüfe ob diese Sequenz bereits Teil einer gefundenen Konversation ist
                is_subset = False
                for existing_conv in found_conversations:
                    existing_messages = existing_conv['user_messages']

                    # Prüfe ob user_messages eine Teilmenge von existing_messages ist
                    if len(user_messages) <= len(existing_messages):
                        if user_messages == existing_messages[:len(user_messages)]:
                            is_subset = True
                            break

                if not is_subset:
                    # Neue Konversation gefunden!
                    conversation_id = f"conv_{len(found_conversations) + 1}_{timestamp.strftime('%Y%m%d_%H%M%S')}"

                    # Erstelle Messages für alle User-Nachrichten
                    messages = []
                    full_history_data = history_data

                    for i, user_msg in enumerate(user_messages):
                        # Finde die entsprechende Claude-Antwort
                        claude_response = "[Keine Claude-Antwort gefunden]"

                        # Suche in der history_data nach der Assistant-Antwort nach dieser User-Nachricht
                        for j, msg in enumerate(full_history_data):
                            if (msg.get('role') == 'user' and
                                msg.get('content', '').strip() == user_msg):
                                # Finde die nächste Assistant-Nachricht
                                for k in range(j + 1, len(full_history_data)):
                                    if full_history_data[k].get('role') == 'assistant':
                                        claude_response = full_history_data[k].get('content', '').strip()
                                        break
                                break

                        # Finde Kontext-Dokumente für diese spezifische User-Nachricht
                        context_docs = []
                        # Suche nach dem HISTORY-Block, in dem diese User-Nachricht die letzte war
                        target_msg_count = i + 1  # Nachricht i+1 von 1-basiert

                        # Durchsuche alle HISTORY-Blöcke um den Block zu finden, der genau target_msg_count User-Nachrichten hat
                        for search_block in history_blocks:
                            try:
                                # Parse diesen HISTORY-Block
                                search_content = search_block['content']
                                search_history_marker = search_content.find('[HISTORY][')
                                if search_history_marker == -1:
                                    continue

                                search_json_start = search_history_marker + len('[HISTORY][')
                                search_json_part = search_content[search_json_start:].strip()

                                if not search_json_part.startswith('['):
                                    if search_json_part.startswith('{'):
                                        search_json_part = '[' + search_json_part
                                    else:
                                        continue

                                # Robuste JSON-Extraktion für search block
                                bracket_count = 0
                                in_string = False
                                escape_next = False
                                json_end = len(search_json_part)

                                for k, char in enumerate(search_json_part):
                                    if escape_next:
                                        escape_next = False
                                        continue
                                    if char == '\\' and in_string:
                                        escape_next = True
                                        continue
                                    if char == '"' and not escape_next:
                                        in_string = not in_string
                                    if not in_string:
                                        if char == '[':
                                            bracket_count += 1
                                        elif char == ']':
                                            bracket_count -= 1
                                            if bracket_count == 0:
                                                json_end = k + 1
                                                break

                                search_json_str = search_json_part[:json_end]
                                search_history_data = json.loads(search_json_str)

                                # Zähle User-Nachrichten in diesem Block
                                search_user_messages = [msg.get('content', '').strip() for msg in search_history_data if msg.get('role') == 'user']

                                # Prüfe ob dieser Block genau die richtige Anzahl User-Nachrichten hat
                                # und ob die letzte Nachricht mit unserer User-Nachricht übereinstimmt
                                if (len(search_user_messages) == target_msg_count and
                                    len(search_user_messages) > 0 and
                                    search_user_messages[-1] == user_msg):
                                    # Das ist der richtige Block für diese User-Nachricht!
                                    context_docs = self.extract_context_documents(search_content)
                                    break

                            except (json.JSONDecodeError, ValueError):
                                continue

                        message = Message(
                            timestamp=timestamp,
                            user_message=user_msg,
                            claude_response=claude_response,
                            context_documents=context_docs
                        )
                        messages.append(message)

                    conversation = Conversation(
                        chat_id=conversation_id,
                        first_message=user_messages[0],
                        start_time=timestamp,
                        message_count=len(user_messages),
                        messages=messages
                    )

                    found_conversations.append({
                        'user_messages': user_messages,
                        'conversation': conversation,
                        'timestamp': timestamp
                    })

                    print(f"✓ Neue Konversation gefunden: {len(user_messages)} Nachrichten, Start: {timestamp_str}")

            except (json.JSONDecodeError, ValueError) as e:
                continue

        # Sortiere Konversationen chronologisch (älteste zuerst) und füge zu self.conversations hinzu
        found_conversations.sort(key=lambda x: x['timestamp'])

        self.conversations = {}
        for i, conv_data in enumerate(found_conversations):
            conv_id = f"conversation_{i+1}"
            self.conversations[conv_id] = conv_data['conversation']

        print(f"\nAnalyse abgeschlossen. {len(self.conversations)} Konversationen gefunden.")
    
    def display_conversations_overview(self) -> None:
        """Zeigt eine Übersicht aller Konversationen"""
        print("\n" + "="*80)
        print("KONVERSATIONEN ÜBERSICHT")
        print("="*80)
        
        if not self.conversations:
            print("Keine Konversationen gefunden.")
            return
        
        sorted_conversations = sorted(
            self.conversations.values(), 
            key=lambda c: c.start_time
        )
        
        for i, conv in enumerate(sorted_conversations, 1):
            print(f"{i:2d}. [{conv.start_time.strftime('%Y-%m-%d %H:%M:%S')}] "
                  f"({conv.message_count} Nachrichten)")
            print(f"    Erste Nachricht: {conv.first_message[:80]}...")
            print(f"    Chat-ID: {conv.chat_id}")
            print()
    
    def display_conversation_details(self, conversation: Conversation) -> None:
        """Zeigt Details einer spezifischen Konversation"""
        print(f"\n{'='*80}")
        print(f"KONVERSATION DETAILS - {conversation.chat_id}")
        print(f"{'='*80}")
        print(f"Start: {conversation.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Nachrichten: {conversation.message_count}")
        print()
        
        for i, message in enumerate(conversation.messages, 1):
            print(f"{i:2d}. [{message.timestamp.strftime('%H:%M:%S')}] USER:")
            print(f"    {message.user_message}")
            if message.context_documents:
                print(f"    Kontext-Dokumente: {len(message.context_documents)}")
                for doc in message.context_documents:
                    print(f"      - {doc['title']} (ID: {doc['id']})")
            print("-" * 60)
    
    def display_message_context(self, message: Message) -> None:
        """Zeigt den Kontext einer spezifischen Nachricht"""
        print(f"\n{'='*80}")
        print("NACHRICHT KONTEXT")
        print(f"{'='*80}")
        print(f"Zeitpunkt: {message.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"User: {message.user_message}")
        print(f"Claude: {message.claude_response}")
        print()
        
        if not message.context_documents:
            print("Keine Kontext-Dokumente gefunden.")
            return
        
        print(f"Kontext-Dokumente ({len(message.context_documents)}):")
        for i, doc in enumerate(message.context_documents, 1):
            print(f"{i:2d}. {doc['title']} (ID: {doc['id']})")
    
    def display_document_content(self, document: Dict[str, str]) -> None:
        """Zeigt den Inhalt eines Dokuments"""
        print(f"\n{'='*80}")
        print(f"DOKUMENT: {document['title']}")
        print(f"{'='*80}")
        print(f"ID: {document['id']}")
        print()
        print(document['content'])
    
    def run_interactive_mode(self) -> None:
        """Startet den interaktiven Modus"""
        while True:
            self.display_conversations_overview()
            
            try:
                choice = input("\nKonversation auswählen (Nummer) oder 'q' zum Beenden: ").strip()
                
                if choice.lower() == 'q':
                    break
                
                conv_index = int(choice) - 1
                sorted_conversations = sorted(
                    self.conversations.values(), 
                    key=lambda c: c.start_time
                )
                
                if 0 <= conv_index < len(sorted_conversations):
                    selected_conversation = sorted_conversations[conv_index]
                    self.explore_conversation(selected_conversation)
                else:
                    print("Ungültige Auswahl.")
                    
            except (ValueError, KeyboardInterrupt):
                break
    
    def explore_conversation(self, conversation: Conversation) -> None:
        """Erkundet eine spezifische Konversation"""
        while True:
            self.display_conversation_details(conversation)
            
            try:
                choice = input("\nNachricht auswählen (Nummer), 'b' für zurück: ").strip()
                
                if choice.lower() == 'b':
                    break
                
                msg_index = int(choice) - 1
                if 0 <= msg_index < len(conversation.messages):
                    selected_message = conversation.messages[msg_index]
                    self.explore_message(selected_message, conversation, msg_index)
                else:
                    print("Ungültige Auswahl.")
                    
            except (ValueError, KeyboardInterrupt):
                break
    
    def explore_message(self, message: Message, conversation: Conversation, message_index: int) -> None:
        """Erkundet eine spezifische Nachricht"""
        while True:
            self.display_message_context(message)

            # Erstelle Eingabeaufforderung basierend auf verfügbaren Optionen
            prompt_parts = []
            if message.context_documents:
                prompt_parts.append("Dokument auswählen (Nummer)")

            has_next = message_index < len(conversation.messages) - 1
            if has_next:
                prompt_parts.append("'n' für nächste Nachricht")

            prompt_parts.append("'b' für zurück")
            prompt = f"\n{', '.join(prompt_parts)}: "

            if not message.context_documents and not has_next:
                input("\nDrücke Enter um zurückzukehren...")
                break

            try:
                choice = input(prompt).strip()

                if choice.lower() == 'b':
                    break
                elif choice.lower() == 'n' and has_next:
                    # Springe zur nächsten Nachricht
                    next_message = conversation.messages[message_index + 1]
                    self.explore_message(next_message, conversation, message_index + 1)
                    break
                elif choice.isdigit():
                    doc_index = int(choice) - 1
                    if 0 <= doc_index < len(message.context_documents):
                        selected_document = message.context_documents[doc_index]
                        self.display_document_content(selected_document)
                        input("\nDrücke Enter um zurückzukehren...")
                    else:
                        print("Ungültige Auswahl.")
                else:
                    print("Ungültige Eingabe.")

            except (ValueError, KeyboardInterrupt):
                break

def main():
    """
    Hauptfunktion des RagFlow Log Analyzers
    
    USAGE:
    python3 analyze.py [logfile_path]
    
    Standard-Logfile: ragflow-logs/ragflow_server.log
    
    FUNKTIONALITÄT:
    1. Parst RagFlow-Logfiles und extrahiert Konversationen
    2. Verknüpft HISTORY-Blöcke mit POST-Requests über Timestamps
    3. Bietet interaktive Navigation durch Konversationen
    4. Zeigt RAG-Kontext-Dokumente für jede Nachricht
    
    ERWEITERUNGSMÖGLICHKEITEN:
    - Export in verschiedene Formate (JSON, CSV, HTML)
    - Filterung nach Zeiträumen oder Keywords
    - Statistiken über Konversationslängen
    - Volltext-Suche in Konversationen
    - Integration mit anderen Log-Quellen
    """
    parser = argparse.ArgumentParser(description='RagFlow Log Analyzer')
    parser.add_argument('logfile', nargs='?', default='ragflow-logs/ragflow_server.log',
                       help='Pfad zum Logfile (Standard: ragflow-logs/ragflow_server.log)')
    
    args = parser.parse_args()
    
    print("RagFlow Log Analyzer")
    print("=" * 70)
    print(f"Logfile: {args.logfile}")
    
    if not os.path.exists(args.logfile):
        print(f"Datei nicht gefunden: {args.logfile}")
        return
    
    # Analyzer initialisieren und Logfile analysieren
    analyzer = RagFlowLogAnalyzer(args.logfile)
    analyzer.analyze_logfile()
    
    # Falls keine Konversationen gefunden wurden
    if not analyzer.conversations:
        print("\nKeine Konversationen konnten verknüpft werden.")
        print("Mögliche Gründe:")
        print("- HISTORY-Blöcke und POST-Requests sind zeitlich zu weit auseinander")
        print("- JSON in HISTORY-Blöcken ist fehlerhaft")
        print("- Unerwartetes Logfile-Format")
        return
    
    # Optional: Interaktiven Modus starten (nur wenn TTY verfügbar)
    try:
        analyzer.run_interactive_mode()
    except (EOFError, KeyboardInterrupt):
        # Falls kein interaktiver Modus möglich, zeige wenigstens die Übersicht
        analyzer.display_conversations_overview()
        print("\nInteraktiver Modus nicht verfügbar oder beendet.")
    
    print("Auf Wiedersehen!")

if __name__ == "__main__":
    main()

