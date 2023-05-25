// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

#import "ort_checkpoint_internal.h"

#include <optional>
#include <string>
#include <variant>
#import "cxx_api.h"

#import "error_utils.h"

NS_ASSUME_NONNULL_BEGIN

@implementation ORTCheckpoint {
  std::optional<Ort::CheckpointState> _checkpoint;
}

- (nullable instancetype)initWithCXXAPIOrtCheckPointFromPath:(NSString*)path
                                                       error:(NSError**)error {
  if ((self = [super init]) == nil) {
    return nil;
  }

  try {
    _checkpoint = Ort::CheckpointState::LoadCheckpoint(path.UTF8String);
    return self;
  }
  ORT_OBJC_API_IMPL_CATCH_RETURNING_NULLABLE(error)
}

+ (nullable instancetype)loadCheckpointFromPath:(NSString*)path
                                          error:(NSError**)error {
  return [[ORTCheckpoint alloc] initWithCXXAPIOrtCheckPointFromPath:path
                                                              error:error];
}

- (BOOL)saveCheckpointToPath:(NSString*)path
          withOptimizerState:(BOOL)includeOptimizerState
                       error:(NSError**)error {
  try {
    Ort::CheckpointState::SaveCheckpoint([self CXXAPIOrtCheckpoint], path.UTF8String, includeOptimizerState);
    return YES;
  }
  ORT_OBJC_API_IMPL_CATCH_RETURNING_BOOL(error)
}

- (BOOL)addPropertyWithName:(NSString*)name
                   intValue:(int64_t)value
                      error:(NSError**)error {
  try {
    [self CXXAPIOrtCheckpoint].AddProperty(name.UTF8String, value);
    return YES;
  }
  ORT_OBJC_API_IMPL_CATCH_RETURNING_BOOL(error)
}

- (BOOL)addPropertyWithName:(NSString*)name
                 floatValue:(float)value
                      error:(NSError**)error {
  try {
    [self CXXAPIOrtCheckpoint].AddProperty(name.UTF8String, value);
    return YES;
  }
  ORT_OBJC_API_IMPL_CATCH_RETURNING_BOOL(error)
}

- (BOOL)addPropertyWithName:(NSString*)name
                stringValue:(NSString*)value
                      error:(NSError**)error {
  try {
    [self CXXAPIOrtCheckpoint].AddProperty(name.UTF8String, value.UTF8String);
    return YES;
  }
  ORT_OBJC_API_IMPL_CATCH_RETURNING_BOOL(error)
}

- (nullable NSString*)getStringPropertyWithName:(NSString*)name error:(NSError**)error {
  Ort::Property value;
  try {
    value = [self CXXAPIOrtCheckpoint].GetProperty(name.UTF8String);
  }
  ORT_OBJC_API_IMPL_CATCH_RETURNING_NULLABLE(error)

  if (std::holds_alternative<std::string>(value)) {
    return [NSString stringWithUTF8String:std::get<std::string>(value).c_str()];
  } else {
    NSString* errorMessage = [NSString stringWithFormat:@"Property '%@' is not a string.", name];
    ORTSaveCodeAndDescriptionToError(1, errorMessage, error);
    return nil;
  }
}

- (int64_t)getIntPropertyWithName:(NSString*)name error:(NSError**)error {
  Ort::Property value;
  try {
    value = [self CXXAPIOrtCheckpoint].GetProperty(name.UTF8String);
  }
  ORT_OBJC_API_IMPL_CATCH(error, 0)

  if (std::holds_alternative<int64_t>(value)) {
    return std::get<int64_t>(value);
  } else {
    NSString* errorMessage = [NSString stringWithFormat:@"Property '%@' is not an integer.", name];
    ORTSaveCodeAndDescriptionToError(1, errorMessage, error);
    return 0;
  }
}

- (float)getFloatPropertyWithName:(NSString*)name error:(NSError**)error {
  Ort::Property value;
  try {
    value = [self CXXAPIOrtCheckpoint].GetProperty(name.UTF8String);
  }
  ORT_OBJC_API_IMPL_CATCH(error, 0.0)

  if (std::holds_alternative<float>(value)) {
    return std::get<float>(value);
  } else {
    NSString* errorMessage = [NSString stringWithFormat:@"Property '%@' is not a float.", name];
    ORTSaveCodeAndDescriptionToError(1, errorMessage, error);
    return 0.0;
  }
}

- (Ort::CheckpointState&)CXXAPIOrtCheckpoint {
  return *_checkpoint;
}

@end

NS_ASSUME_NONNULL_END
// ./build.sh --config RelWithDebInfo --build_shared_lib --parallel --use_xcode --enable_training --build_objc  --cmake_extra_defines CMAKE_OSX_ARCHITECTURES=x86_64
