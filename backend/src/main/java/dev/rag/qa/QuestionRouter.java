package dev.rag.qa;

import org.springframework.stereotype.Component;

import java.util.regex.Pattern;

/**
 * 用户提问意图轻量分流路由器。
 * <p>
 * 通过高效的确定性规则与正则匹配，在进入昂贵的向量检索和模型调用链之前，
 * 对问候寒暄（SMALL_TALK）或非支持的通用娱乐指令（UNSUPPORTED）进行前置短路拦截，
 * 仅将真正的知识库问答请求路由至完整 RAG 流水线（KNOWLEDGE_QA）。
 */
@Component
public final class QuestionRouter {
    /**
     * 意图分类路由分支枚举。
     */
    public enum Route {
        /** 闲聊、问候与礼貌用语（如你好、谢谢、再见等） */
        SMALL_TALK,
        /** 知识库检索与生成问答（正常业务 RAG 流水线） */
        KNOWLEDGE_QA,
        /** 超出当前系统知识库定位的无关请求（如天气查询、讲笑话、写诗等） */
        UNSUPPORTED
    }

    /**
     * 路由裁决结果数据载体。
     *
     * @param route 命中的路由分支枚举 {@link Route}
     * @param reason 做出此裁决的规则或原因标识（如 GREETING_RULE, DEFAULT_KNOWLEDGE_ROUTE 等）
     */
    public record Decision(Route route, String reason) {
    }

    private static final Pattern SMALL_TALK = Pattern.compile("(?iu)^\\s*(?:你好|您好|hi|hello|谢谢|感谢|再见)[！!。.?？,，\\s]*$");
    private static final Pattern UNSUPPORTED = Pattern.compile("(?iu)^\\s*(?:请)?(?:讲个笑话|写一首诗|查询?今天的?天气|今天天气怎么样)[！!。.?？,，\\s]*$");

    /**
     * 对用户输入问题进行意图分类并给出路由裁决。
     *
     * @param question 用户输入的提问文本
     * @return 路由决策对象 {@link Decision}
     */
    public Decision classify(String question) {
        if (SMALL_TALK.matcher(question).matches()) return new Decision(Route.SMALL_TALK, "GREETING_RULE");
        if (UNSUPPORTED.matcher(question).matches()) return new Decision(Route.UNSUPPORTED, "GENERAL_ASSISTANT_RULE");
        return new Decision(Route.KNOWLEDGE_QA, "DEFAULT_KNOWLEDGE_ROUTE");
    }
}
