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
  //define trap enter,analog0
  pinMode(trapPin, INPUT);
   //turn on serial output
  Serial.begin(9600);
}

void loop()
{
  //do a little light show
  while(!firstRun)
  {
    	firstRun=initLED();
  }
  
  while(!raceStarted)
  {
    serialInteraction();
  }
  while (raceStarted)
  {
    if (digitalRead(trapPin) == HIGH)
    {
      if (currentLap == 0)
      {
        reactionTime = millis() - startTime;
        Serial.println("Reaction Time: " + String(reactionTime) + " ms");
        lapCount++;
      }

      if (currentLap > 0)
      {
        lapTime = millis() - startTime - lastLapTime;
        lastLapTime += lapTime;
        Serial.println("Lap " + String(currentLap) + " Time: " + String(lapTime) + " ms");
        lapCount++;
      }

      if (currentLap >= lapCount)
      {
        raceStarted=false;
        Serial.println("Race Finished!");
      }
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
  Serial.println("Do You want to start a race? (Y/N)");
  if (Serial.available() > 0)
  {
    input = Serial.readStringUntil('\n');
    input = input.trim();
  }

  if (input == "Y" || input == "y") 
  {
    Serial.println("How Many Laps? (1-500)");
    if (Serial.available() > 0)
    {
      lapCount = Serial.readStringUntil('\n').toInt();
    }
    Serial.println("You have selected " + String(lapCount) + " laps.");
    Serial.println("Ready to Start? (Y/N)");
    if (Serial.available() > 0)
    {
      input = Serial.readStringUntil('\n');
      
    } 
    if (input == "Y" || input == "y") 
      {
        startRace();
      }
  }
}
