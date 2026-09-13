package dev.rag.qa;

import org.springframework.stereotype.Component;
import java.util.regex.Pattern;

@Component
public final class QuestionRouter {
 public enum Route { SMALL_TALK, KNOWLEDGE_QA, UNSUPPORTED }
 public record Decision(Route route,String reason){}

 private static final Pattern SMALL_TALK=Pattern.compile("(?iu)^\\s*(?:你好|您好|hi|hello|谢谢|感谢|再见)[！!。.?？,，\\s]*$");
 private static final Pattern UNSUPPORTED=Pattern.compile("(?iu)^\\s*(?:请)?(?:讲个笑话|写一首诗|查询?今天的?天气|今天天气怎么样)[！!。.?？,，\\s]*$");

 public Decision classify(String question){
  if(SMALL_TALK.matcher(question).matches())return new Decision(Route.SMALL_TALK,"GREETING_RULE");
  if(UNSUPPORTED.matcher(question).matches())return new Decision(Route.UNSUPPORTED,"GENERAL_ASSISTANT_RULE");
  return new Decision(Route.KNOWLEDGE_QA,"DEFAULT_KNOWLEDGE_ROUTE");
 }
}
