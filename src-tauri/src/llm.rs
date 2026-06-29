//! LLM 客户端 —— OpenAI 兼容 HTTP API
//! 对应 TS `main/coordinator/llm.ts`

use crate::store::types::LlmConfig;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String, // "system" | "user" | "assistant"
    pub content: String,
}

/// 默认 LLM 配置：从环境变量读取
pub fn get_default_config() -> LlmConfig {
    LlmConfig {
        apiKey: std::env::var("OPENAI_API_KEY")
            .or_else(|_| std::env::var("ANTHROPIC_API_KEY"))
            .unwrap_or_default(),
        baseUrl: std::env::var("OPENAI_BASE_URL")
            .unwrap_or_else(|_| "https://api.openai.com/v1".into()),
        model: std::env::var("LLM_MODEL").unwrap_or_else(|_| "glm-5.1".into()),
        temperature: 0.0,
        maxTokens: 4096,
    }
}

#[derive(Deserialize)]
struct ChatCompletionResponse {
    choices: Vec<ChatChoice>,
}

#[derive(Deserialize)]
struct ChatChoice {
    message: ChatChoiceMessage,
}

#[derive(Deserialize)]
struct ChatChoiceMessage {
    content: String,
}

/// 调用 LLM，返回纯文本
pub async fn chat_completion(config: &LlmConfig, messages: Vec<ChatMessage>) -> anyhow::Result<String> {
    let client = reqwest::Client::new();
    let url = format!("{}/chat/completions", config.baseUrl.trim_end_matches('/'));
    let body = serde_json::json!({
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.maxTokens,
    });
    let resp = client
        .post(&url)
        .bearer_auth(&config.apiKey)
        .json(&body)
        .send()
        .await?;
    if !resp.status().is_success() {
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        anyhow::bail!("LLM API error {status}: {text}");
    }
    let data: ChatCompletionResponse = resp.json().await?;
    Ok(data
        .choices
        .into_iter()
        .next()
        .ok_or_else(|| anyhow::anyhow!("LLM 返回空 choices"))?
        .message
        .content)
}

/// 从 LLM 文本响应中提取首个 JSON 对象 `{...}`
pub fn extract_json(raw: &str) -> Option<serde_json::Value> {
    let start = raw.find('{')?;
    // 从最后一个 } 往前找，配对首个 {
    let mut depth = 0i32;
    let mut end = None;
    for (i, ch) in raw[start..].char_indices() {
        match ch {
            '{' => depth += 1,
            '}' => {
                depth -= 1;
                if depth == 0 {
                    end = Some(start + i);
                    break;
                }
            }
            _ => {}
        }
    }
    let end = end?;
    serde_json::from_str(&raw[start..=end]).ok()
}
