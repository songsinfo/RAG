package dev.rag.knowledge;
import dev.rag.common.ApiException;
import dev.rag.common.Json;
import org.junit.jupiter.api.Test;
import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
class ParserDeadlineTest {
 @Test void pollingAndTaskLossCannotRefreshDeadline(){var cp=new HashMap<String,Object>();ParserDeadline.check(cp,null,1000,240);cp.put("externalTaskId","task-a");var restored=Json.object(Json.write(cp));ParserDeadline.check(restored,null,240999,240);restored.remove("externalTaskId");var e=assertThrows(ApiException.class,()->ParserDeadline.check(restored,null,241000,240));assertEquals("PARSER_TIMEOUT",e.code);assertEquals(1000L,((Number)restored.get(ParserDeadline.START)).longValue());}
 @Test void existingTasksUseOriginalCreationTime(){var cp=new HashMap<String,Object>();cp.put("externalTaskId","old-task");LocalDateTime created=LocalDateTime.ofEpochSecond(1,0,ZoneOffset.UTC);assertThrows(ApiException.class,()->ParserDeadline.check(cp,created,241000,240));assertEquals(1000L,cp.get(ParserDeadline.START));}
}
