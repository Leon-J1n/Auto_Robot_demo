add_executable(test_algorithm_integration_c test_algorithms.c)
target_link_libraries(test_algorithm_integration_c PRIVATE sim::algorithms)
set_target_properties(test_algorithm_integration_c PROPERTIES LINKER_LANGUAGE CXX)
add_custom_command(TARGET test_algorithm_integration_c POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
            $<TARGET_FILE:sim_algorithms_c>
            $<TARGET_FILE_DIR:test_algorithm_integration_c>)
