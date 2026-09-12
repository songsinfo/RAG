package dev.rag.common;

import org.flywaydb.core.Flyway;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import javax.sql.DataSource;

@Configuration
public class DatabaseMigrationConfig {

 @Bean(initMethod = "migrate")
 public Flyway flyway(DataSource dataSource, @Value("${spring.flyway.locations:classpath:db/migration}") String locations) {
  return Flyway.configure()
   .dataSource(dataSource)
   .locations(locations)
   .load();
 }
}
