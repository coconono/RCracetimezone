// C++ code
//
//#include <Arduino.h>

//variables for pins and setup
bool firstRun=false;
bool raceStarted=false;
int redLED = 13;
int yellowLED = 12;
int greenLED = 11;
int trapPin = A0;

//race variables
int lapCount = -1;
int currentLap = -1;
int reactionTime = -1;
int lastLapTime = 0;
int lapTime = -1;
int startTime = -1;

//interactive variables
String input = "";

void setup()
{
  //initialize pins here
  //define red LED, digital13
  pinMode(redLED, OUTPUT);
  //define yellow LED, digital12
  pinMode(yellowLED, OUTPUT);
  //define green LED, , digital11
  pinMode(greenLED, OUTPUT);
  //define trap input pin,analog0
  pinMode(trapPin, INPUT);
   //turn on serial output
  Serial.begin(9600);
}

void loop()
{
  //do a little light show
  while (!firstRun)
  {
    	firstRun=initLED();
  }
  
  while (!raceStarted)
  {
    serialInteraction();
  }

  while (raceStarted)
  {
    if (digitalRead(trapPin) == HIGH)
    {
      digitalWrite(trapPin, LOW); //imediately reset the pin
      Serial.println("Trap Triggered!");
      Serial.println("lapCount: " + String(currentLap) + " / " + String(lapCount));
      //trap triggered but its the first lap, so log as reaction time
      if (currentLap == 0)
      {
        reactionTime = millis() - startTime;
        Serial.println("Reaction Time: " + String(reactionTime) + " ms");
        currentLap++;
      }
      //trap triggered and its not the first lap, so log lap time
      else if (currentLap > 0)
      {
        lapTime = millis() - startTime - lastLapTime;
        lastLapTime += lapTime;
        Serial.println("Lap " + String(currentLap) + " Time: " + String(lapTime) + " ms");
        currentLap++;
      }

      if (currentLap > lapCount)
      {
        raceStarted=false;
        Serial.println("Race Finished!");
      }
      delay(2000); //debounce delay
    }
  }
}

bool initLED()
{
    //red 3 times
    for (int i=0; i<3; i++)
    {
      digitalWrite(redLED, HIGH);
      delay(1000);
      digitalWrite(redLED, LOW);
      delay(1000);
    }
    //yellow 2 times
    for (int i=0; i<2; i++)
    {
      digitalWrite(yellowLED, HIGH);
      delay(1000);
      digitalWrite(yellowLED, LOW);
      delay(1000);
    }
    //green once
    for (int i=0; i<1; i++)
    {
      digitalWrite(greenLED, HIGH);
      delay(1000);
      digitalWrite(greenLED, LOW);
      delay(1000);
    }
    return true;
}

void startRace()
{
    //red 1 times
    digitalWrite(redLED, HIGH);
    delay(1000);
    //yellow 1 times
    digitalWrite(yellowLED, HIGH);
    delay(1000);
    //green 1 times
    digitalWrite(greenLED, HIGH);
    delay(1000);
    Serial.println("Race Started!");
    startTime = millis();
    raceStarted=true;
    currentLap=0;
}

void serialInteraction()
{
  // Wait for user to start race
  input = "";
  while (true) {
    Serial.println("Do You want to start a race? (Y/N)");
    while (Serial.available() == 0) {
      // Wait for input
      delay(10);
    }
    input = Serial.readStringUntil('\n');
    if (input == "Y" || input == "y") {
      break;
    } else if (input == "N" || input == "n") {
      Serial.println("Race not started. Send Y to start.");
    } else {
      Serial.println("Invalid input. Please enter Y or N.");
    }
  }

  // Ask for number of laps
  lapCount = -1;
  while (lapCount < 1 || lapCount > 500) {
    Serial.println("How Many Laps? (1-500)");
    while (Serial.available() == 0) {
      delay(10);
    }
    String lapInput = Serial.readStringUntil('\n');
    lapCount = lapInput.toInt();
    if (lapCount < 1 || lapCount > 500) {
      Serial.println("Invalid lap count. Please enter a number between 1 and 500.");
    }
  }
  Serial.println("You have selected " + String(lapCount) + " laps.");

  // Confirm ready to start
  while (true) {
    Serial.println("Ready to Start? (Y/N)");
    while (Serial.available() == 0) {
      delay(10);
    }
    input = Serial.readStringUntil('\n');
    if (input == "Y" || input == "y") {
      startRace();
      break;
    } else if (input == "N" || input == "n") {
      Serial.println("Race cancelled. Send Y to start again.");
      break;
    } else {
      Serial.println("Invalid input. Please enter Y or N.");
    }
  }
}
