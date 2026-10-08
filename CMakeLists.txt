cmake_minimum_required(VERSION 3.16)
project(nanovro VERSION 0.1.0 LANGUAGES C)
add_library(nanovro src/nanovro.c)
target_include_directories(nanovro PUBLIC $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include> $<INSTALL_INTERFACE:include>)
target_compile_features(nanovro PUBLIC c_std_99)
if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(nanovro PRIVATE -Wall -Wextra -Wpedantic -Wconversion)
endif()
option(NANOVRO_BUILD_TESTS "Build host regression tests" OFF)
option(NANOVRO_TEST_INTEROP "Run Python/Apache Avro interoperability tests" OFF)
if(NANOVRO_TEST_INTEROP AND NOT NANOVRO_BUILD_TESTS)
  message(FATAL_ERROR "NANOVRO_TEST_INTEROP requires NANOVRO_BUILD_TESTS=ON")
endif()
if(NANOVRO_BUILD_TESTS)
  enable_testing()
  find_package(Python3 3.9 REQUIRED COMPONENTS Interpreter)
  add_executable(test_runtime tests/test_runtime.c)
  target_link_libraries(test_runtime PRIVATE nanovro)
  add_executable(test_optimizations tests/test_optimizations.c)
  target_link_libraries(test_optimizations PRIVATE nanovro)
  add_executable(test_portable_arrays tests/test_optimizations.c src/nanovro.c)
  target_include_directories(test_portable_arrays PRIVATE include)
  target_compile_features(test_portable_arrays PRIVATE c_std_99)
  target_compile_definitions(test_portable_arrays PRIVATE NV_DISABLE_ARRAY_BULK_COPY=1)
  foreach(test_target test_runtime test_optimizations test_portable_arrays)
    add_test(NAME ${test_target} COMMAND ${test_target})
  endforeach()
  add_test(NAME generator COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_generator.py)
  if(NANOVRO_TEST_INTEROP)
    if(NOT CMAKE_C_COMPILER_ID MATCHES "GNU|Clang" OR MSVC)
      message(FATAL_ERROR "Interoperability tests require a GCC-compatible compiler (GCC or Clang)")
    endif()
    execute_process(COMMAND ${Python3_EXECUTABLE} -c "import avro.io, avro.schema"
      RESULT_VARIABLE avro_import_result OUTPUT_QUIET ERROR_QUIET)
    if(NOT avro_import_result EQUAL 0)
      message(FATAL_ERROR "Install tests/requirements.txt using ${Python3_EXECUTABLE} -m pip install -r tests/requirements.txt")
    endif()
    foreach(suite interop resolution bundle)
      add_test(NAME ${suite} COMMAND ${CMAKE_COMMAND} -E env
        "CC=${CMAKE_C_COMPILER}" ${Python3_EXECUTABLE}
        ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_${suite}.py)
    endforeach()
  endif()
endif()
option(NANOVRO_BUILD_EXAMPLES "Build runnable host examples" OFF)
if(NANOVRO_BUILD_EXAMPLES)
  enable_testing()
  find_package(Python3 3.9 REQUIRED COMPONENTS Interpreter)
  set(GENERATED ${CMAKE_CURRENT_BINARY_DIR}/generated)
  foreach(SCHEMA telemetry)
    add_custom_command(OUTPUT ${GENERATED}/${SCHEMA}.c ${GENERATED}/${SCHEMA}.h
      COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/generator/nanovro_generator.py
        ${CMAKE_CURRENT_SOURCE_DIR}/examples/${SCHEMA}/${SCHEMA}.avsc -o ${GENERATED}
      DEPENDS generator/nanovro_generator.py examples/${SCHEMA}/${SCHEMA}.avsc VERBATIM)
  endforeach()
  add_executable(telemetry_example examples/telemetry/main.c ${GENERATED}/telemetry.c)
  target_include_directories(telemetry_example PRIVATE ${GENERATED})
  target_link_libraries(telemetry_example PRIVATE nanovro)
  add_test(NAME telemetry_example COMMAND telemetry_example)
  set(RESOLUTION_GENERATED ${GENERATED}/resolution)
  add_custom_command(OUTPUT ${RESOLUTION_GENERATED}/resolve.c ${RESOLUTION_GENERATED}/resolve.h
      ${RESOLUTION_GENERATED}/resolve_reader.c ${RESOLUTION_GENERATED}/resolve_reader.h
      ${RESOLUTION_GENERATED}/resolve_writer.c ${RESOLUTION_GENERATED}/resolve_writer.h
      ${RESOLUTION_GENERATED}/connections.c ${RESOLUTION_GENERATED}/connections.h
      ${RESOLUTION_GENERATED}/connections_writer0.c ${RESOLUTION_GENERATED}/connections_writer0.h
      ${RESOLUTION_GENERATED}/connections_writer1.c ${RESOLUTION_GENERATED}/connections_writer1.h
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/generator/nanovro_resolver.py
      ${CMAKE_CURRENT_SOURCE_DIR}/examples/schema_resolution/old.avsc
      ${CMAKE_CURRENT_SOURCE_DIR}/examples/schema_resolution/current.avsc
      --name resolve -o ${RESOLUTION_GENERATED}
    COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/generator/nanovro_resolver.py
      ${CMAKE_CURRENT_SOURCE_DIR}/examples/schema_resolution/writers.json
      ${CMAKE_CURRENT_SOURCE_DIR}/examples/schema_resolution/current.avsc
      --bundle --name connections --reader-name resolve_reader -o ${RESOLUTION_GENERATED}
    DEPENDS generator/nanovro_resolver.py generator/nanovro_generator.py
      examples/schema_resolution/old.avsc examples/schema_resolution/current.avsc
      examples/schema_resolution/writers.json VERBATIM)
  add_executable(resolution_example examples/schema_resolution/main.c
    ${RESOLUTION_GENERATED}/resolve.c ${RESOLUTION_GENERATED}/resolve_reader.c ${RESOLUTION_GENERATED}/resolve_writer.c
    ${RESOLUTION_GENERATED}/connections.c ${RESOLUTION_GENERATED}/connections_writer0.c ${RESOLUTION_GENERATED}/connections_writer1.c)
  target_include_directories(resolution_example PRIVATE ${RESOLUTION_GENERATED})
  target_link_libraries(resolution_example PRIVATE nanovro)
  add_test(NAME resolution_example COMMAND resolution_example)
endif()
install(TARGETS nanovro ARCHIVE DESTINATION lib)
install(FILES include/nanovro.h DESTINATION include)
install(PROGRAMS generator/nanovro_generator.py DESTINATION bin)
install(PROGRAMS generator/nanovro_resolver.py DESTINATION bin)
