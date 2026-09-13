package dev.rag.knowledge;

import dev.rag.common.ApiException;
import java.time.*;
import java.util.Map;

/** Persisted start survives polling, lease takeover and a lost external task. */
final class ParserDeadline {
 static final String START="externalTaskStartedAt";
 static void check(Map<String,Object> checkpoint,Object jobCreatedAt,long now,int timeoutSeconds){
  if(timeoutSeconds<1||timeoutSeconds>3600)throw new IllegalArgumentException("Parser deadline must be 1..3600 seconds");
  Object start=checkpoint.get(START);
  if(start==null){
   // Older persisted tasks predate the explicit clock; never give them a fresh timeout on every poll.
   long created=jobCreatedAt instanceof java.sql.Timestamp t?t.toInstant().toEpochMilli():jobCreatedAt instanceof LocalDateTime t?t.toInstant(ZoneOffset.UTC).toEpochMilli():now;
   start=checkpoint.containsKey("externalTaskId")?created:now;
   checkpoint.put(START,start);
  }
  long started=((Number)start).longValue();
  if(now-started>=timeoutSeconds*1000L)throw new ApiException(422,"PARSER_TIMEOUT","解析任务超过总等待时限，请检查解析服务后重试");
 }
 private ParserDeadline(){}
}
