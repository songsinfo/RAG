package dev.rag.qa;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

class QuestionRouterTest {
 private final QuestionRouter router=new QuestionRouter();

 @Test void exactGreetingSkipsKnowledgeRetrieval(){assertEquals(QuestionRouter.Route.SMALL_TALK,router.classify("你好！").route());}
 @Test void businessQuestionUsesKnowledgeRoute(){assertEquals(QuestionRouter.Route.KNOWLEDGE_QA,router.classify("优惠券核销失败怎么排查？").route());}
 @Test void clearGeneralAssistantRequestIsUnsupported(){assertEquals(QuestionRouter.Route.UNSUPPORTED,router.classify("讲个笑话").route());}
 @Test void greetingInsideBusinessQuestionDoesNotHideTheQuestion(){assertEquals(QuestionRouter.Route.KNOWLEDGE_QA,router.classify("你好，优惠券为什么不可用？").route());}
}
