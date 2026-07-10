//! LLM 客户端 + LLM 配置（greenfield 重写）
//!
//! OpenAI 兼容 chat completion。
//! 关键修复：extract_json 支持纯 JSON 与 ```json 围栏，且字符串字面量内的大括号
//! 不再破坏配对（扫描时跟踪字符串状态，跳过字面量内容）。

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String, // "system" | "user" | "assistant"
    pub content: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[allow(non_snake_case)]
pub struct LlmConfig {
    #[serde(default)]
    pub apiKey: String,
    #[serde(default)]
    pub baseUrl: String,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub temperature: f64,
    #[serde(default)]
    pub maxTokens: i64,
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

/// 从 LLM 文本响应中提取首个 JSON 对象 `{...}`。
///
/// 修复旧 brace-counting 的脆性：
/// - 先剥 ```json / ``` 围栏（LLM 常无视"不要 markdown"指令）。
/// - 扫描时跟踪字符串字面量（`"`），字面量内的大括号不计入配对，
///   并处理转义 `\"`。旧实现遇到 content 里含 `}` 就解析崩溃。
pub fn extract_json(raw: &str) -> Option<serde_json::Value> {
    let mut s = raw.trim();
    // 剥 markdown 围栏
    if s.starts_with("```") {
        if let Some(nl) = s.find('\n') {
            s = &s[nl + 1..];
        }
        if let Some(end) = s.rfind("```") {
            s = &s[..end];
        }
        s = s.trim();
    }
    let start = s.find('{')?;
    let bytes = s.as_bytes();
    let mut depth = 0i32;
    let mut in_str = false;
    let mut escape = false;
    let mut end: Option<usize> = None;
    let mut i = start;
    while i < bytes.len() {
        let ch = bytes[i];
        if in_str {
            if escape {
                escape = false;
            } else if ch == b'\\' {
                escape = true;
            } else if ch == b'"' {
                in_str = false;
            }
        } else {
            match ch {
                b'"' => in_str = true,
                b'{' => depth += 1,
                b'}' => {
                    depth -= 1;
                    if depth == 0 {
                        end = Some(i);
                        break;
                    }
                }
                _ => {}
            }
        }
        i += 1;
    }
    let end = end?;
    serde_json::from_str(&s[start..=end]).ok()
}
