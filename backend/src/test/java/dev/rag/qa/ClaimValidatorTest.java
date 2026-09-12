package dev.rag.qa;
import org.junit.jupiter.api.Test;
import dev.rag.common.ApiException;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
class ClaimValidatorTest {
 Map<String,Object> answer(String id,int end){return Map.of("status","COMPLETED","claims",List.of(Map.of("id","C1","text","需要重试","evidence",List.of(Map.of("evidenceId",id,"start",0,"end",end)))));}
 @Test void inventedCitationIsRejected(){assertThrows(ApiException.class,()->ClaimValidator.validate(answer("E2",1),Map.of("E1","原文")));}
 @Test void offsetsAreCodePoints(){assertDoesNotThrow(()->ClaimValidator.validate(answer("E1",2),Map.of("E1","𠀀文")));assertThrows(ApiException.class,()->ClaimValidator.validate(answer("E1",3),Map.of("E1","𠀀文")));}
 @Test void freeAnswerCannotSmuggleUncitedFacts(){var a=new HashMap<>(answer("E1",1));a.put("answer","额外事实");assertThrows(ApiException.class,()->ClaimValidator.validate(a,Map.of("E1","文")));}
 @Test void completionNeedsClaim(){assertThrows(ApiException.class,()->ClaimValidator.validate(Map.of("status","COMPLETED","claims",List.of()),Map.of()));}
}
