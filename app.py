import os
from flask import Flask, request, jsonify
import requests
from supabase import create_client, Client
from dotenv import load_dotenv

# Carrega variáveis locais se existir arquivo .env (Desenvolvimento)
load_dotenv()

app = Flask(__name__)

# --- CONFIGURAÇÕES ---
# As variáveis de ambiente devem ser configuradas no painel do Render
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "face_doctor_segredo")
META_TOKEN = os.environ.get("META_TOKEN")
META_PHONE_ID = os.environ.get("META_PHONE_ID")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Inicializa Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

def log_system_event(level, source, message, metadata=None):
    """Registra eventos e erros na tabela system_logs do Supabase"""
    if not supabase: return
    try:
        supabase.table('system_logs').insert({
            "level": level,
            "source": source,
            "message": str(message),
            "metadata": metadata
        }).execute()
    except Exception as e:
        print(f"❌ Erro crítico ao logar no Supabase: {e}")

def enviar_mensagem_whatsapp(numero, texto):
    """Envia mensagem via WhatsApp API Oficial"""
    if not META_TOKEN or not META_PHONE_ID:
        error_msg = "Credenciais da Meta não configuradas."
        print(f"❌ {error_msg}")
        log_system_event("ERROR", "API_SEND", error_msg)
        return

    url = f"https://graph.facebook.com/v19.0/{META_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": texto}
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        print(f"✅ Mensagem enviada para {numero}")
    except Exception as e:
        error_msg = f"Erro ao enviar no WhatsApp para {numero}: {str(e)}"
        print(f"❌ {error_msg}")
        log_system_event("ERROR", "API_SEND", error_msg, {"response": str(getattr(e, 'response', 'No response'))})

def get_or_create_lead(phone, name="Cliente WhatsApp"):
    """Busca um lead pelo telefone ou cria um novo se não existir"""
    if not supabase: return None

    try:
        # 1. Tenta buscar lead existente
        response = supabase.table('leads').select("*").eq('phone', phone).execute()
        if response.data and len(response.data) > 0:
            return response.data[0] # Retorna o lead encontrado
        
        # 2. Se não existir, busca a primeira etapa do funil (stage_id)
        stages_res = supabase.table('pipeline_stages').select("id").order('position').limit(1).execute()
        first_stage_id = stages_res.data[0]['id'] if stages_res.data else None

        if not first_stage_id:
            error_msg = "Nenhuma etapa de funil encontrada para criar o lead."
            print(f"❌ {error_msg}")
            log_system_event("ERROR", "SYSTEM", error_msg)
            return None

        # 3. Cria novo lead
        new_lead_data = {
            "name": name,
            "phone": phone,
            "stage_id": first_stage_id,
            "unread_count": 1,
            "custom_fields": {}
        }
        create_res = supabase.table('leads').insert(new_lead_data).execute()
        print(f"✨ Novo lead criado: {phone}")
        return create_res.data[0]

    except Exception as e:
        print(f"❌ Erro no Supabase (Lead): {e}")
        log_system_event("ERROR", "SYSTEM", f"Falha ao criar/buscar lead: {str(e)}")
        return None

def salvar_mensagem(lead_id, content, direction='inbound'):
    """Salva a mensagem na tabela 'messages' do Supabase"""
    if not supabase or not lead_id: return

    try:
        msg_data = {
            "lead_id": lead_id,
            "content": content,
            "direction": direction,
            "type": "text",
            "status": "read" if direction == 'inbound' else 'sent'
        }
        supabase.table('messages').insert(msg_data).execute()
        
        # Atualiza o lead
        if direction == 'inbound':
            supabase.table('leads').update({
                "last_message_at": "now()",
                "last_message_content": content
            }).eq('id', lead_id).execute()
            
        print(f"💾 Mensagem salva no banco para Lead ID: {lead_id}")
    except Exception as e:
        print(f"❌ Erro ao salvar mensagem: {e}")
        log_system_event("ERROR", "SYSTEM", f"Falha ao salvar mensagem: {str(e)}")

@app.route('/', methods=['GET'])
def health_check():
    return "WhatsApp CRM Webhook is Running!", 200

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    # --- ROTA GET: VERIFICAÇÃO ---
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode and token:
            if mode == 'subscribe' and token == VERIFY_TOKEN:
                return challenge, 200
            else:
                return 'Token inválido', 403
        return 'Hello World', 200

    # --- ROTA POST: RECEBIMENTO ---
    elif request.method == 'POST':
        data = request.json
        try:
            if 'object' in data and 'entry' in data:
                entry = data['entry'][0]
                changes = entry['changes'][0]
                value = changes['value']
                
                if 'messages' in value:
                    message = value['messages'][0]
                    
                    if message['type'] == 'text':
                        numero_cliente = message['from']
                        corpo_mensagem = message['text']['body']
                        contacts = value.get('contacts', [{}])
                        nome_perfil = contacts[0].get('profile', {}).get('name', 'Cliente')
                        
                        print(f"\n📩 Nova mensagem de {nome_perfil} ({numero_cliente}): {corpo_mensagem}")

                        if supabase:
                            lead = get_or_create_lead(numero_cliente, nome_perfil)
                            if lead:
                                salvar_mensagem(lead['id'], corpo_mensagem, 'inbound')

        except Exception as e:
            print(f"Erro ao processar webhook: {e}")
            log_system_event("ERROR", "WEBHOOK_META", f"Falha no processamento: {str(e)}", {"payload": data})

        return jsonify({'status': 'success'}), 200

@app.route('/send_message', methods=['POST'])
def api_send_message():
    data = request.json
    phone = data.get('phone')
    text = data.get('text')
    lead_id = data.get('lead_id')
    
    if not phone or not text:
        return jsonify({"error": "Missing phone or text"}), 400
        
    enviar_mensagem_whatsapp(phone, text)
    
    if lead_id:
        salvar_mensagem(lead_id, text, 'outbound')
        
    return jsonify({"status": "sent"}), 200

if __name__ == '__main__':
    # O Render fornece a porta na variável de ambiente PORT
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
